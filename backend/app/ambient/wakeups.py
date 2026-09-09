"""Agent-scheduled wakeups — heartbeat sense H2 (spec §17.4, milestone M22).

The platform, not the model, owns the clock: delays are clamped to
[60s, 24h], each routine is capped at 5 pending and a per-day budget, and a
done-guard at fire time expires wakeups whose reason a later completed run
has superseded. Fired wakeups become ordinary routine-addressed events —
the sanctioned self-wake path past the §17.3a no-self-trigger guard.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select

from app.db import get_session_factory
from app.models import AmbientWakeup, Run

logger = structlog.get_logger("ambient")

WAKEUP_MIN_DELAY_S = 60
WAKEUP_MAX_DELAY_S = 24 * 3600
WAKEUP_MAX_PENDING = 5


class WakeupCapError(RuntimeError):
    """A wakeup cap or clamp refused the schedule request."""


async def schedule_wakeup(
    routine_id: UUID,
    *,
    delay_s: int | None = None,
    at: str | None = None,
    reason: str,
    payload: dict[str, Any] | None = None,
    created_by: str = "agent",
    run_id: UUID | None = None,
    now: datetime | None = None,
) -> AmbientWakeup:
    """Insert one pending wakeup, clamped and capped. Raises WakeupCapError
    when a cap refuses it — callers surface that, never retry-loop."""
    now = now or datetime.now(UTC)
    if at is not None:
        due = datetime.fromisoformat(at)
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        delay = (due - now).total_seconds()
    else:
        delay = float(delay_s if delay_s is not None else WAKEUP_MIN_DELAY_S)
    clamped = min(max(delay, WAKEUP_MIN_DELAY_S), WAKEUP_MAX_DELAY_S)
    due_at = now + timedelta(seconds=clamped)

    from app.registry_cache import get_cache

    daily_cap = int(await get_cache().setting("ambient_wakeups_per_routine_per_day"))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session_factory()() as session:
        pending = (
            await session.execute(
                select(func.count()).where(
                    AmbientWakeup.routine_id == routine_id,
                    AmbientWakeup.status == "pending",
                )
            )
        ).scalar_one()
        if pending >= WAKEUP_MAX_PENDING:
            raise WakeupCapError(
                f"routine already has {pending} pending wakeups (cap {WAKEUP_MAX_PENDING})"
            )
        today = (
            await session.execute(
                select(func.count()).where(
                    AmbientWakeup.routine_id == routine_id,
                    AmbientWakeup.created_at >= midnight,
                )
            )
        ).scalar_one()
        if today >= daily_cap:
            raise WakeupCapError(f"routine hit the wakeup cap of {daily_cap} per day")
        wakeup = AmbientWakeup(
            routine_id=routine_id,
            run_id=run_id,
            due_at=due_at,
            reason=reason,
            payload=payload,
            created_by=created_by,
        )
        session.add(wakeup)
        await session.commit()
        await session.refresh(wakeup)
    from app import obs

    obs.AMBIENT_OPS.labels(kind="wakeup", status="scheduled").inc()
    logger.info(
        "ambient_wakeup_scheduled",
        tier="ambient",
        kind="wakeup",
        routine=str(routine_id),
        due_at=due_at.isoformat(),
    )
    return wakeup


async def cancel_wakeup(wakeup_id: UUID) -> bool:
    """Cancel one pending wakeup. Only pending rows are cancellable."""
    async with get_session_factory()() as session:
        wakeup = await session.get(AmbientWakeup, wakeup_id)
        if wakeup is None or wakeup.status != "pending":
            return False
        wakeup.status = "cancelled"
        await session.commit()
    return True


async def _superseded(wakeup: AmbientWakeup, now: datetime) -> bool:
    """Done-guard (spec §17.4): a completed run of the same routine that
    STARTED after the wakeup was scheduled supersedes its reason — the work
    the wakeup was booked for already happened."""
    _ = now
    if wakeup.routine_id is None:
        return False
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                select(func.count()).where(
                    Run.trigger.isnot(None),
                    Run.trigger["routine_id"].as_string() == str(wakeup.routine_id),
                    Run.status == "completed",
                    Run.started_at >= wakeup.created_at,
                )
            )
        ).scalar_one()
    return bool(count)


async def fire_due_wakeups(now: datetime | None = None) -> int:
    """Tick evaluator: due pending wakeups become routine-addressed events
    (source='wakeup'); superseded ones expire. Returns events emitted."""
    from app.ambient.store import ChainGuardError, emit_event

    now = now or datetime.now(UTC)
    fired = 0
    async with get_session_factory()() as session:
        due = list(
            (
                await session.execute(
                    select(AmbientWakeup).where(
                        AmbientWakeup.status == "pending", AmbientWakeup.due_at <= now
                    )
                )
            ).scalars()
        )
    for wakeup in due:
        if wakeup.routine_id is None or await _superseded(wakeup, now):
            outcome = "expired"
        else:
            try:
                event = await emit_event(
                    kind="agent_wakeup",
                    source="wakeup",
                    payload={"reason": wakeup.reason, **(wakeup.payload or {})},
                    dedupe_key=f"wakeup:{wakeup.id}",
                    routine_id=wakeup.routine_id,
                )
            except ChainGuardError as exc:
                logger.warning("ambient_wakeup_guarded", wakeup=str(wakeup.id), error=str(exc))
                outcome = "expired"
            else:
                outcome = "fired" if event is not None else "expired"
        async with get_session_factory()() as session:
            row = await session.get(AmbientWakeup, wakeup.id)
            if row is not None and row.status == "pending":
                row.status = outcome
                if outcome == "fired":
                    row.fired_at = now
                await session.commit()
        if outcome == "fired":
            fired += 1
        else:
            from app import obs

            obs.AMBIENT_OPS.labels(kind="wakeup", status="expired").inc()
    return fired
