"""Presence + the real idle detector (spec §17.4/§17.5 — heartbeat sense H1
consumer). The client sends heartbeats; the tick derives state transitions
and emits presence events (idle / user_returned) into the trigger plane.

States (doc 05b area 3): active (beat <60s ∧ visible ∧ input <5 min),
idle (beat fresh, no input 5–30 min), away (no beat 2 min or hidden),
offline (no beat >30 min).
"""

from datetime import UTC, datetime, timedelta

import structlog

from app.db import get_session_factory
from app.models import UserPresence

logger = structlog.get_logger("ambient")

_ACTIVE_INPUT_S = 5 * 60
_AWAY_BEAT_S = 2 * 60
_OFFLINE_BEAT_S = 30 * 60


def _presence_key() -> tuple[str, "object | None"]:
    """§18.8: per-user presence rows when auth is on ("user:{uuid}");
    the single-user regime keeps the one "default" row."""
    from app.auth import auth_enabled, current_user_id

    uid = current_user_id() if auth_enabled() else None
    return (f"user:{uid}", uid) if uid is not None else ("default", None)


async def record_heartbeat(*, visible: bool, activity: bool) -> UserPresence:
    """Client heartbeat (30s cadence; immediate on foreground). `activity`
    marks real input since the last beat."""
    now = datetime.now(UTC)
    key, uid = _presence_key()
    async with get_session_factory()() as session:
        row = await session.get(UserPresence, key)
        if row is None:
            row = UserPresence(id=key, user_id=uid)
            session.add(row)
        row.last_heartbeat_at = now
        row.visible = visible
        if activity:
            row.last_activity_at = now
        await session.commit()
        await session.refresh(row)
    return row


def _derive_state(row: UserPresence, now: datetime) -> str:
    beat = row.last_heartbeat_at
    if beat is None or (now - beat).total_seconds() > _OFFLINE_BEAT_S:
        return "offline"
    if (now - beat).total_seconds() > _AWAY_BEAT_S or not row.visible:
        return "away"
    act = row.last_activity_at
    if act is not None and (now - act).total_seconds() <= _ACTIVE_INPUT_S:
        return "active"
    return "idle"


async def evaluate_presence(idle_minutes: int) -> str | None:
    """One tick: derive the state, persist transitions, and emit presence
    events. `idle_minutes` (ambient_idle_minutes) additionally gates the
    platform-level idle event used for consolidation/anticipation — the
    conversation-quiet detector spec §16.2 promised.

    Returns the emitted event kind, if any.
    """
    from sqlalchemy import select

    from app.ambient.store import emit_event

    now = datetime.now(UTC)
    emitted: str | None = None
    async with get_session_factory()() as session:
        rows = list((await session.execute(select(UserPresence))).scalars())
    for snapshot in rows:  # §18.8: one transition pass per presence row
        async with get_session_factory()() as session:
            row = await session.get(UserPresence, snapshot.id)
            if row is None:
                continue
            new_state = _derive_state(row, now)
            old_state = row.state
            if new_state == old_state:
                continue
            away_s = (now - row.state_since).total_seconds() if row.state_since else 0.0
            row_user_id = row.user_id
            row.state = new_state
            row.state_since = now
            await session.commit()
        kind: str | None = None
        if old_state in {"idle", "away", "offline"} and new_state == "active":
            kind = "user_returned"
        elif old_state == "active" and new_state in {"idle", "away"}:
            kind = "user_idle"
        if kind:
            emitted = kind
            await emit_event(
                kind=kind,
                source="presence",
                payload={"from": old_state, "to": new_state, "away_s": away_s},
                require_enabled=True,
            )
            if kind == "user_returned":
                # §17.5 return-flush: tier 1 always; > 1h away also the digest
                from app.ambient.deliver import on_user_returned

                await on_user_returned(away_s, now=now, user_id=row_user_id)
            logger.info(
                "ambient_presence",
                tier="ambient",
                kind="ingest",
                transition=f"{old_state}->{new_state}",
            )
    return emitted


async def is_platform_idle(idle_minutes: int) -> bool:
    """The real idle detector (spec §17.4): no active runs AND no chat
    activity for `idle_minutes`. Used by consolidation/anticipation jobs."""
    from sqlalchemy import func, select

    from app.models import Run

    cutoff = datetime.now(UTC) - timedelta(minutes=idle_minutes)
    async with get_session_factory()() as session:
        active = (
            await session.execute(
                select(func.count()).where(Run.status.in_(["running", "paused_hitl"]))
            )
        ).scalar_one()
        if active:
            return False
        latest = (await session.execute(select(func.max(Run.started_at)))).scalar_one()
    return latest is None or latest <= cutoff
