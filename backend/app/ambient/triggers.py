"""Trigger evaluators (spec §17.2 — milestone M21): schedules, adaptive
pollers, state conditions. All deterministic; all run on the ambient tick;
each emits typed events into the store — the decision plane does the rest.

Poll sources and state probes are pluggable registries (name → coroutine):
native/MCP-backed sources register in code; tests register fakes. The tick
never calls a model here.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from croniter import croniter
from sqlalchemy import select

from app.ambient.store import ChainGuardError, emit_event
from app.db import get_session_factory
from app.models import Routine, StandingIntent

logger = structlog.get_logger("ambient")

# poll source contract (§18.3): (watermark, config) -> (new_items, new_watermark)
PollSource = Callable[
    [str | None, dict[str, Any]], Awaitable[tuple[list[dict[str, Any]], str | None]]
]
_POLL_SOURCES: dict[str, PollSource] = {}
_POLL_SHAPES: dict[str, str] = {}

# state probe contract (§18.3): (config) -> float (current value of the quantity)
StateProbe = Callable[[dict[str, Any]], Awaitable[float]]
_STATE_PROBES: dict[str, StateProbe] = {}
_PROBE_SHAPES: dict[str, str] = {}


def register_poll_source(name: str, fn: PollSource | None, config_shape: str = "") -> None:
    if fn is None:
        _POLL_SOURCES.pop(name, None)
        _POLL_SHAPES.pop(name, None)
    else:
        _POLL_SOURCES[name] = fn
        _POLL_SHAPES[name] = config_shape


def register_state_probe(name: str, fn: StateProbe | None, config_shape: str = "") -> None:
    if fn is None:
        _STATE_PROBES.pop(name, None)
        _PROBE_SHAPES.pop(name, None)
    else:
        _STATE_PROBES[name] = fn
        _PROBE_SHAPES[name] = config_shape


def registered_poll_sources() -> set[str]:
    return set(_POLL_SOURCES)


def registered_state_probes() -> set[str]:
    return set(_STATE_PROBES)


def poll_source_specs() -> dict[str, str]:
    """name → config shape, for the watch compiler's prompt (§18.3)."""
    return dict(_POLL_SHAPES)


def state_probe_specs() -> dict[str, str]:
    return dict(_PROBE_SHAPES)


# ── schedules (spec §17.2: cron / interval / once, UTC + stagger) ─────


def _schedule_due(
    trig: dict[str, Any], last_fired: datetime | None, stagger_s: int, now: datetime
) -> bool:
    anchor = last_fired
    if trig.get("type") == "interval":
        seconds = max(int(trig.get("seconds", 3600)), 60)
        if anchor is None:
            return True
        return (now - anchor).total_seconds() >= seconds
    if trig.get("type") == "once":
        at = datetime.fromisoformat(str(trig.get("at")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return now >= (at + timedelta(seconds=stagger_s)) and last_fired is None
    if trig.get("type") == "cron":
        expr = str(trig.get("cron", ""))
        if not croniter.is_valid(expr):
            return False
        base = anchor or (now - timedelta(days=1))
        next_at = croniter(expr, base).get_next(datetime)
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=UTC)
        return bool(now >= (next_at + timedelta(seconds=stagger_s)))
    return False


async def evaluate_schedules(now: datetime | None = None) -> int:
    """Emit one schedule event per due routine trigger. Missed ticks fire
    once on recovery, never replayed N times (watermark = last_fired_at)."""
    now = now or datetime.now(UTC)
    fired = 0
    async with get_session_factory()() as session:
        routines = list(
            (await session.execute(select(Routine).where(Routine.status == "active"))).scalars()
        )
    for routine in routines:
        for i, trig in enumerate(routine.triggers or []):
            if trig.get("type") not in {"interval", "once", "cron"}:
                continue
            if not _schedule_due(trig, routine.last_fired_at, routine.stagger_offset_s, now):
                continue
            slot = now.strftime("%Y%m%d%H%M")
            try:
                event = await emit_event(
                    kind="routine_schedule",
                    source="schedule",
                    payload={"trigger_index": i, "trigger": trig},
                    dedupe_key=f"sched:{routine.id}:{i}:{slot}",
                    routine_id=routine.id,
                )
            except ChainGuardError as exc:
                logger.warning("ambient_schedule_capped", routine=str(routine.id), error=str(exc))
                continue
            if event is None:
                continue
            fired += 1
            async with get_session_factory()() as session:
                row = await session.get(Routine, routine.id)
                if row is not None:
                    row.last_fired_at = now
                    if trig.get("type") == "once":
                        trigs = list(row.triggers or [])
                        trigs[i] = {**trig, "type": "once_fired"}
                        row.triggers = trigs
                    await session.commit()
    return fired


# ── adaptive pollers (spec §17.2: AIMD backoff, reset on activity) ────


async def poll_due_intents(now: datetime | None = None) -> int:
    """Poll standing intents whose adaptive interval has elapsed. New items
    become events; quiet checks back the interval off (×multiplier up to
    max); any hit resets it to base."""
    now = now or datetime.now(UTC)
    emitted = 0
    async with get_session_factory()() as session:
        intents = list(
            (
                await session.execute(
                    select(StandingIntent).where(
                        StandingIntent.status == "active",
                        StandingIntent.condition_type == "event",
                    )
                )
            ).scalars()
        )
    for intent in intents:
        compiled = intent.compiled or {}
        source_name = (compiled.get("poll") or {}).get("source")
        if not source_name or source_name not in _POLL_SOURCES:
            continue
        if intent.expires_at is not None and now >= intent.expires_at:
            async with get_session_factory()() as session:
                row = await session.get(StandingIntent, intent.id)
                if row is not None:
                    row.status = "expired"
                    await session.commit()
            continue
        # near-due tightening (spec §18.1): a deadline inside 2× the adaptive
        # interval polls at base cadence — AIMD never causes a missed deadline
        effective_interval = intent.current_interval_s
        if (
            intent.expires_at is not None
            and (intent.expires_at - now).total_seconds() <= 2 * intent.current_interval_s
        ):
            effective_interval = min(effective_interval, intent.base_interval_s)
        last = intent.last_checked_at
        if last is not None and (now - last).total_seconds() < effective_interval:
            continue
        poll_config = dict((compiled.get("poll") or {}).get("config") or {})
        try:
            items, new_watermark = await _POLL_SOURCES[source_name](intent.watermark, poll_config)
        except Exception as exc:  # noqa: BLE001 — a broken source never kills the tick
            logger.warning("ambient_poll_failed", intent=str(intent.id), error=str(exc))
            continue
        async with get_session_factory()() as session:
            row = await session.get(StandingIntent, intent.id)
            if row is None:
                continue
            row.last_checked_at = now
            if items:
                row.watermark = new_watermark
                row.consecutive_quiet = 0
                row.current_interval_s = row.base_interval_s  # reset on activity
            else:
                row.consecutive_quiet += 1
                row.current_interval_s = min(
                    int(row.current_interval_s * row.backoff_multiplier), row.max_interval_s
                )
            await session.commit()
        for item in items[:20]:  # bounded per check
            event = await emit_event(
                kind="intent_poll_item",
                source="poll",
                payload={"item": item},  # UNTRUSTED
                intent_id=intent.id,
            )
            if event is not None:
                emitted += 1
    return emitted


# ── state conditions (spec §17.2: state ≠ event; edge detection) ─────


async def evaluate_state_conditions(now: datetime | None = None) -> int:
    """State intents fire on the FALSE→TRUE edge of `probe op value`, never
    while the condition merely holds (the TAP event/state lesson)."""
    now = now or datetime.now(UTC)
    _ = now
    emitted = 0
    async with get_session_factory()() as session:
        intents = list(
            (
                await session.execute(
                    select(StandingIntent).where(
                        StandingIntent.status == "active",
                        StandingIntent.condition_type == "state",
                    )
                )
            ).scalars()
        )
    for intent in intents:
        compiled = intent.compiled or {}
        probe_name = compiled.get("probe")
        if not probe_name or probe_name not in _STATE_PROBES:
            continue
        try:
            value = await _STATE_PROBES[probe_name](dict(compiled.get("config") or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ambient_probe_failed", intent=str(intent.id), error=str(exc))
            continue
        op = compiled.get("op", ">=")
        threshold = float(compiled.get("value", 0))
        holds = (
            value >= threshold
            if op == ">="
            else value <= threshold
            if op == "<="
            else value == threshold
        )
        was_holding = intent.watermark == "holding"
        if holds and not was_holding:
            event = await emit_event(
                kind="state_condition",
                source="internal",
                payload={"probe": probe_name, "value": value, "op": op, "threshold": threshold},
                intent_id=intent.id,
            )
            if event is not None:
                emitted += 1
        if holds != was_holding:
            async with get_session_factory()() as session:
                row = await session.get(StandingIntent, intent.id)
                if row is not None:
                    row.watermark = "holding" if holds else "clear"
                    await session.commit()
    return emitted
