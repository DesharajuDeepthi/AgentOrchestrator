"""CEP-lite composite patterns (spec §17.3a — milestone M21).

Exactly three kinds — sequence (A then B within T), conjunction (A and B
within T), absence (A without B by T) — as pattern_instances keyed by
partition. Absence is an ARMED TIMER fired by the tick, never a continuous
query. Matched/expired patterns emit DERIVED events carrying the full
causation chain, so the §17.3a guards (depth, no-self-trigger, kill switch,
cooldown) apply end-to-end.

Rules live in a standing intent's compiled form:
  {"pattern": {"kind": "sequence"|"conjunction"|"absence",
               "a": {filters}, "b": {filters}, "window_s": N,
               "partition_field": "path.in.payload"?, "cooldown_s": 300}}
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from app.ambient.decide import match_filters
from app.ambient.store import ChainGuardError, emit_event
from app.db import get_session_factory
from app.models import AmbientEvent, PatternInstance, StandingIntent

logger = structlog.get_logger("ambient")

_COOLDOWN_DEFAULT_S = 300
_recent_fires: dict[tuple[str, str], datetime] = {}  # (rule_key, partition) → last fire


def _partition(event: AmbientEvent, spec: dict[str, Any]) -> str:
    field = spec.get("partition_field")
    if not field:
        return ""
    raw: Any = event.payload or {}
    for part in str(field).split("."):
        raw = raw.get(part) if isinstance(raw, dict) else None
    return "" if raw is None else str(raw)


def _cooled_down(rule_key: str, partition: str, spec: dict[str, Any], now: datetime) -> bool:
    cooldown = int(spec.get("cooldown_s", _COOLDOWN_DEFAULT_S))
    last = _recent_fires.get((rule_key, partition))
    return last is None or (now - last).total_seconds() >= cooldown


async def advance_patterns(event: AmbientEvent) -> int:
    """Feed one event through every active pattern rule. Returns derived
    events emitted (matches). Called by the drain before the decision plane."""
    now = datetime.now(UTC)
    emitted = 0
    async with get_session_factory()() as session:
        intents = list(
            (
                await session.execute(
                    select(StandingIntent).where(StandingIntent.status == "active")
                )
            ).scalars()
        )
    # filters see the same data model everywhere in the decision plane:
    # kind, source, and the payload fields (what the watch compiler promises)
    data = {"kind": event.kind, "source": event.source, **(event.payload or {})}
    for intent in intents:
        spec = (intent.compiled or {}).get("pattern")
        if not spec:
            continue
        rule_key = f"intent:{intent.id}"
        kind = spec.get("kind")
        a_match = match_filters(data, spec.get("a", {}).get("filters", []))
        b_match = match_filters(data, spec.get("b", {}).get("filters", []))
        window = timedelta(seconds=int(spec.get("window_s", 3600)))
        partition = _partition(event, spec)

        async with get_session_factory()() as session:
            armed = (
                await session.execute(
                    select(PatternInstance).where(
                        PatternInstance.rule_key == rule_key,
                        PatternInstance.partition_key == partition,
                        PatternInstance.state == "armed",
                    )
                )
            ).scalar_one_or_none()

            if kind in {"sequence", "absence"}:
                if armed is None and a_match:
                    session.add(
                        PatternInstance(
                            rule_key=rule_key,
                            partition_key=partition,
                            a_event_id=event.id,
                            deadline_at=now + window,
                        )
                    )
                    await session.commit()
                    continue
                if armed is not None and b_match:
                    if kind == "sequence":
                        armed.state = "matched"
                        await session.commit()
                        if _cooled_down(rule_key, partition, spec, now):
                            emitted += await _emit_pattern(
                                intent, event, "pattern_matched", rule_key, partition, now
                            )
                    else:  # absence: B arrived in time — expectation satisfied
                        armed.state = "expired"
                        await session.commit()
                    continue
            elif kind == "conjunction":
                if armed is None and (a_match or b_match):
                    session.add(
                        PatternInstance(
                            rule_key=rule_key,
                            partition_key=partition,
                            a_event_id=event.id,
                            deadline_at=now + window,
                        )
                    )
                    await session.commit()
                    continue
                if armed is not None and (a_match or b_match) and armed.a_event_id != event.id:
                    first = await session.get(AmbientEvent, armed.a_event_id)
                    first_data = (
                        {"kind": first.kind, "source": first.source, **(first.payload or {})}
                        if first is not None
                        else {}
                    )
                    first_was_a = first is not None and match_filters(
                        first_data, spec.get("a", {}).get("filters", [])
                    )
                    completes = (first_was_a and b_match) or (not first_was_a and a_match)
                    if completes:
                        armed.state = "matched"
                        await session.commit()
                        if _cooled_down(rule_key, partition, spec, now):
                            emitted += await _emit_pattern(
                                intent, event, "pattern_matched", rule_key, partition, now
                            )
    return emitted


async def _emit_pattern(
    intent: StandingIntent,
    caused_by: AmbientEvent,
    kind: str,
    rule_key: str,
    partition: str,
    now: datetime,
) -> int:
    try:
        event = await emit_event(
            kind=kind,
            source="pattern",
            payload={"rule": rule_key, "partition": partition},
            intent_id=intent.id,
            caused_by=caused_by,
        )
    except ChainGuardError as exc:
        logger.warning("ambient_pattern_guarded", rule=rule_key, error=str(exc))
        return 0
    if event is None:
        return 0
    _recent_fires[(rule_key, partition)] = now
    return 1


async def expire_pattern_deadlines(now: datetime | None = None) -> int:
    """The tick's timer wheel: armed instances past deadline. For absence
    rules the expiry IS the match (A happened, B never came); for
    sequence/conjunction it is just cleanup."""
    now = now or datetime.now(UTC)
    fired = 0
    async with get_session_factory()() as session:
        due = list(
            (
                await session.execute(
                    select(PatternInstance).where(
                        PatternInstance.state == "armed",
                        PatternInstance.deadline_at.isnot(None),
                        PatternInstance.deadline_at <= now,
                    )
                )
            ).scalars()
        )
        for inst in due:
            inst.state = "expired"
        await session.commit()
    for inst in due:
        if not inst.rule_key.startswith("intent:"):
            continue
        async with get_session_factory()() as session:
            intent = await session.get(StandingIntent, UUID(inst.rule_key.removeprefix("intent:")))
            a_event = await session.get(AmbientEvent, inst.a_event_id) if inst.a_event_id else None
        if intent is None:
            continue
        spec = (intent.compiled or {}).get("pattern") or {}
        if spec.get("kind") != "absence":
            continue
        if not _cooled_down(inst.rule_key, inst.partition_key, spec, now):
            continue
        try:
            event = await emit_event(
                kind="pattern_absence",
                source="pattern",
                payload={"rule": inst.rule_key, "partition": inst.partition_key},
                intent_id=intent.id,
                caused_by=a_event,
            )
        except ChainGuardError as exc:
            logger.warning("ambient_pattern_guarded", rule=inst.rule_key, error=str(exc))
            continue
        if event is not None:
            _recent_fires[(inst.rule_key, inst.partition_key)] = now
            fired += 1
    return fired
