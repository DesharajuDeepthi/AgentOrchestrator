"""Ambient event store (spec §17.2/§17.3a) — append-only, guard-checked.

Every event carries chaining provenance (causation/correlation/depth); the
four cascade guards live HERE, at write time, not in prompts:
depth ceiling, no-self-trigger, per-source rate kill switch, dedupe.
NOTIFY is a wake-up ping only — the drain reads the table.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy import text as sql_text

from app.db import get_session_factory
from app.models import AmbientEvent
from app.models.ambient import EVENT_SOURCES

logger = structlog.get_logger("ambient")

NOTIFY_CHANNEL = "ambient_events"
MAX_DEPTH = 4  # §17.3a — far below Salesforce's 16; ambient chains stay short
RULE_KILL_SWITCH_PER_HOUR = 50


class AmbientDisabledError(RuntimeError):
    """Raised when an ambient write is attempted while the mode is dark."""


class ChainGuardError(ValueError):
    """A derived event violated a §17.3a cascade guard."""


async def _enabled() -> bool:
    from app.registry_cache import get_cache

    return bool(await get_cache().setting("ambient_enabled"))


async def emit_event(
    *,
    kind: str,
    source: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    routine_id: UUID | None = None,
    intent_id: UUID | None = None,
    caused_by: AmbientEvent | None = None,
    occurred_at: datetime | None = None,
    require_enabled: bool = True,
) -> AmbientEvent | None:
    """Append one event (UNTRUSTED payload) and ping the drain.

    Returns None when deduped. Raises ChainGuardError on guard violations —
    callers emitting derived events must treat that as a hard stop, not retry.
    """
    if source not in EVENT_SOURCES:
        raise ValueError(f"unknown event source {source!r}")
    if require_enabled and not await _enabled():
        raise AmbientDisabledError("ambient_enabled is off")

    depth = 0
    causation_id: UUID | None = None
    correlation_id: UUID | None = None
    if caused_by is not None:
        depth = int(caused_by.depth) + 1
        causation_id = caused_by.id
        correlation_id = caused_by.correlation_id or caused_by.id
        if depth >= MAX_DEPTH:
            raise ChainGuardError(f"depth {depth} >= {MAX_DEPTH} — cascade stopped")
        # no-self-trigger: a routine may not appear twice in its own chain
        if routine_id is not None and await _routine_in_chain(caused_by, routine_id):
            raise ChainGuardError(f"routine {routine_id} already in causation chain")

    async with get_session_factory()() as session:
        if dedupe_key:
            existing = (
                await session.execute(
                    select(AmbientEvent.id).where(AmbientEvent.dedupe_key == dedupe_key).limit(1)
                )
            ).first()
            if existing is not None:
                logger.info("ambient_event_deduped", tier="ambient", kind="ingest", event_kind=kind)
                return None
        if routine_id is not None:
            hour_ago = datetime.now(UTC) - timedelta(hours=1)
            recent = (
                await session.execute(
                    select(func.count()).where(
                        AmbientEvent.routine_id == routine_id,
                        AmbientEvent.received_at >= hour_ago,
                    )
                )
            ).scalar_one()
            if recent >= RULE_KILL_SWITCH_PER_HOUR:
                raise ChainGuardError(f"kill switch: {recent} events for routine in the last hour")
        event = AmbientEvent(
            kind=kind,
            source=source,
            payload=payload,
            dedupe_key=dedupe_key,
            routine_id=routine_id,
            intent_id=intent_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            depth=depth,
        )
        if occurred_at is not None:
            event.occurred_at = occurred_at
        session.add(event)
        await session.flush()
        if event.correlation_id is None:
            event.correlation_id = event.id  # chain roots correlate to themselves
        await session.execute(
            sql_text("SELECT pg_notify(:channel, :pid)"),
            {"channel": NOTIFY_CHANNEL, "pid": str(event.id)},
        )
        await session.commit()
        await session.refresh(event)
    from app import obs

    obs.AMBIENT_OPS.labels(kind="ingest", status="ok").inc()
    logger.info(
        "ambient_event",
        tier="ambient",
        kind="ingest",
        event_kind=kind,
        source=source,
        depth=depth,
        event_id=str(event.id),
    )
    return event


async def _routine_in_chain(event: AmbientEvent, routine_id: UUID) -> bool:
    """Walk the causation chain (bounded by MAX_DEPTH) looking for routine_id."""
    async with get_session_factory()() as session:
        current: AmbientEvent | None = event
        for _ in range(MAX_DEPTH + 1):
            if current is None:
                return False
            if current.routine_id == routine_id:
                return True
            if current.causation_id is None:
                return False
            current = await session.get(AmbientEvent, current.causation_id)
    return False


async def pending_events(limit: int = 20) -> list[AmbientEvent]:
    """Claim up to `limit` unprocessed events (FOR UPDATE SKIP LOCKED semantics
    are applied by the drain inside its own transaction; this helper is the
    read used by tests and the M20 skeleton)."""
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AmbientEvent)
                    .where(AmbientEvent.verdict.is_(None))
                    .order_by(AmbientEvent.received_at)
                    .limit(limit)
                )
            ).scalars()
        )
    return rows
