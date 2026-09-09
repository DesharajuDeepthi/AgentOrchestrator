"""The decision plane (spec §17.3 — milestone M21): three tiers, strictly
ordered by cost. Tier 1 typed matchers (no model), tier 2 one significance
judgment (extraction role, per-intent override), tier 3 the run (wired in
M22 — here a 'fired' verdict is the hand-off record). Every decision writes
the fire/hold ledger. Silence is the default: anything unmatched holds.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db import get_session_factory
from app.models import AmbientEvent, Routine, Run, StandingIntent

logger = structlog.get_logger("ambient")

_FIRE_SOURCES = {"webhook", "schedule", "manual", "wakeup"}


class SignificanceOutput(BaseModel):
    significant: bool
    urgency: int = Field(ge=1, le=5, default=2)
    reason: Literal["new_information", "matches_watch", "urgent_change", "routine", "noise"] = (
        "routine"
    )


def match_filters(payload: dict[str, Any] | None, filters: list[dict[str, Any]]) -> bool:
    """Tier-1 typed matchers: field + operator, the §17.3 set."""
    data = payload or {}
    for f in filters:
        raw: Any = data
        for part in str(f.get("field", "")).split("."):
            raw = raw.get(part) if isinstance(raw, dict) else None
        value = "" if raw is None else str(raw)
        expect = str(f.get("value", ""))
        op = f.get("op", "equals")
        ok = (
            value == expect
            if op == "equals"
            else expect in value
            if op == "contains"
            else value.startswith(expect)
            if op == "starts_with"
            else value in [str(v) for v in f.get("values", [])]
            if op == "one_of"
            else re.search(expect, value) is not None
            if op == "regex"
            else False
        )
        if not ok:
            return False
    return True


async def _runs_today_cap_reached() -> bool:
    from app.registry_cache import get_cache

    cap = int(await get_cache().setting("ambient_runs_per_day"))
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                select(func.count()).where(Run.trigger.isnot(None), Run.started_at >= midnight)
            )
        ).scalar_one()
    return count >= cap


# §18.1 judge cost accounting: a single registerable hook receives each
# judge call's usage_metadata — the M24 harness and tests install one
_judge_usage_hook: Any | None = None


def register_judge_usage_hook(fn: Any | None) -> None:
    global _judge_usage_hook
    _judge_usage_hook = fn


async def _judge_significance(intent: StandingIntent, event: AmbientEvent) -> SignificanceOutput:
    """Tier 2: ONE structured call, extraction role unless the intent
    overrides. Any failure means held — silence is the default. Runs with
    include_raw so real token usage feeds the cost accounting (§18.1)."""
    from app.llm import ModelParams, get_model
    from app.prompts import load_prompt
    from app.registry_cache import get_cache

    cache = get_cache()
    ref = (
        intent.judge_model_ref
        or await cache.setting("memory_extraction_model")
        or await cache.setting("default_model")
    )
    model = get_model(str(ref), ModelParams(effort="low"))
    structured = model.with_structured_output(SignificanceOutput, include_raw=True)
    prompt = load_prompt("ambient_significance").format(
        watch=intent.text,
        predicate=intent.semantic_predicate or "(none)",
        event=str(event.payload)[:2000],
    )
    out = await structured.ainvoke(prompt)
    assert isinstance(out, dict)
    parsed = out.get("parsed")
    if not isinstance(parsed, SignificanceOutput):
        raise ValueError(f"judge parse failed: {out.get('parsing_error')}")
    usage = dict(getattr(out.get("raw"), "usage_metadata", None) or {})
    from app import obs

    obs.AMBIENT_JUDGE_TOKENS.labels(direction="input").inc(int(usage.get("input_tokens", 0)))
    obs.AMBIENT_JUDGE_TOKENS.labels(direction="output").inc(int(usage.get("output_tokens", 0)))
    if _judge_usage_hook is not None:
        try:
            _judge_usage_hook(usage)
        except Exception:  # noqa: BLE001 — a broken hook never breaks the judge
            logger.warning("judge_usage_hook_failed")
    logger.info(
        "ambient_judge_usage",
        tier="ambient",
        kind="judge",
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
    return parsed


async def process_event(event: AmbientEvent) -> tuple[str, str, dict[str, Any]]:
    """The drain's processor. Returns (verdict, reason, decision). The DRAIN
    writes all three onto the row it holds locked — a processor must never
    touch ambient_events itself (FOR UPDATE self-deadlock, found live)."""
    decision: dict[str, Any] = {"tier": 1}
    try:
        verdict, reason, decision = await _decide(event)
    except Exception as exc:  # noqa: BLE001 — fail-open to held
        verdict, reason = "held", f"decision error: {exc}"
    from app import obs

    obs.AMBIENT_OPS.labels(kind=verdict, status="ok").inc()
    logger.info(
        "ambient_decision",
        tier="ambient",
        kind=verdict,
        event_kind=event.kind,
        reason=reason[:120],
    )
    return (verdict, reason, decision)


async def _decide(event: AmbientEvent) -> tuple[str, str, dict[str, Any]]:
    decision: dict[str, Any] = {"tier": 1, "urgency": 2}

    # routine-addressed events (schedule/webhook/manual/wakeup)
    if event.routine_id is not None and event.source in _FIRE_SOURCES:
        async with get_session_factory()() as session:
            routine = await session.get(Routine, event.routine_id)
        if routine is None or routine.status != "active":
            return ("dropped", "routine missing or not active", decision)
        trig = (event.payload or {}).get("trigger") or {}
        filters = trig.get("filters") or []
        if filters and not match_filters((event.payload or {}).get("payload"), filters):
            return ("held", "tier-1 filters did not match", decision)
        # §18.5: webhook fires carry no embedded trigger, so they are matched
        # against the ROUTINE's stored webhook-trigger filters — any matching
        # webhook trigger admits the fire (an empty filter list matches all)
        if not filters and event.source == "webhook":
            webhook_trigs = [
                t
                for t in (routine.triggers or [])
                if isinstance(t, dict) and t.get("type") == "webhook"
            ]
            if webhook_trigs and not any(
                match_filters((event.payload or {}).get("payload"), t.get("filters") or [])
                for t in webhook_trigs
            ):
                return ("held", "webhook trigger filters did not match", decision)
        if await _runs_today_cap_reached():
            return ("dropped", "ambient_runs_per_day cap reached", decision)
        decision["fired_for"] = "routine"
        return ("fired", "routine trigger matched", decision)

    # HITL aging (spec §17.4/§17.5): the question rides the digest; the
    # paused checkpoint stays resumable — no run, just an outbox row
    if event.source == "internal" and event.kind == "hitl_aged":
        return await _queue_hitl_delivery(event, decision)

    # intent-addressed events (poll items, state conditions, patterns)
    if event.intent_id is not None:
        async with get_session_factory()() as session:
            intent = await session.get(StandingIntent, event.intent_id)
        if intent is None or intent.status != "active":
            return ("dropped", "intent missing or not active", decision)
        filters = (intent.compiled or {}).get("filters") or []
        # §18.3: the compiler writes poll filters over ITEM fields, so a
        # poll item is unwrapped into the unified data model {kind, source,
        # **item} before matching — the raw payload keeps its fence wrapper
        data = dict(event.payload or {})
        item = data.get("item")
        if isinstance(item, dict):
            data = {"kind": event.kind, "source": event.source, **item}
        if filters and not match_filters(data, filters):
            return ("held", "tier-1 filters did not match", decision)
        return await _intent_fire(intent, event, decision)

    # unaddressed events: standing watches compiled to event filters may
    # still claim them (spec §17.3 tier 1 → tier 2 on match)
    matched = await _match_event_intents(event, decision)
    if matched is not None:
        return matched
    if event.source in {"presence", "internal"}:
        return ("held", "informational event (no routine/intent addressee)", decision)
    return ("held", "no addressee", decision)


async def _intent_fire(
    intent: StandingIntent, event: AmbientEvent, decision: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    """Shared tier-2 → tier-3 tail for intent fires: judge (if the intent
    has a semantic predicate), then the daily cap, then the fired verdict."""
    if intent.semantic_predicate:
        decision["tier"] = 2
        try:
            judged = await _judge_significance(intent, event)
        except Exception as exc:  # noqa: BLE001 — judge failure ⇒ silence
            return ("held", f"significance judge failed: {exc}", decision)
        decision["urgency"] = judged.urgency
        decision["judge_reason"] = judged.reason
        if not judged.significant:
            return ("held", "judged not significant", decision)
        # §17.7: a learner-raised judge bar for this watch
        floor = int((intent.budget or {}).get("min_urgency", 0))
        if judged.urgency < floor:
            return (
                "held",
                f"urgency {judged.urgency} below intent min_urgency {floor}",
                decision,
            )
    if await _runs_today_cap_reached():
        return ("dropped", "ambient_runs_per_day cap reached", decision)
    decision["fired_for"] = "intent"
    decision["intent_id"] = str(intent.id)
    return ("fired", "intent condition met", decision)


async def _match_event_intents(
    event: AmbientEvent, decision: dict[str, Any]
) -> tuple[str, str, dict[str, Any]] | None:
    """Standing watches compiled as event filters (ambient.watch mode
    'events') evaluated against an unaddressed event. Filters see `kind`,
    `source`, and the payload fields. First match wins; None = no claim."""
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
    data = {"kind": event.kind, "source": event.source, **(event.payload or {})}
    for intent in intents:
        compiled = intent.compiled or {}
        if compiled.get("match") != "events":
            continue
        filters = compiled.get("filters") or []
        if not filters or not match_filters(data, filters):
            continue
        outcome = await _intent_fire(intent, event, dict(decision))
        if outcome[0] == "fired":
            outcome[2]["matched_watch"] = intent.text[:120]
        return outcome
    return None


async def _queue_hitl_delivery(
    event: AmbientEvent, decision: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    from app.ambient.deliver import add_delivery
    from app.registry_cache import get_cache

    run_id_raw = (event.payload or {}).get("run_id")
    if not run_id_raw:
        return ("dropped", "hitl_aged event without run_id", decision)
    timeout_h = int(await get_cache().setting("ambient_hitl_timeout_h"))
    await add_delivery(
        run_id=UUID(str(run_id_raw)),
        category="hitl",
        tier=2,
        urgency=3,
        title="[ambient] a run is waiting on your input",
        body=(
            f"Run {run_id_raw} has been paused on a human-input gate for over "
            f"{timeout_h}h. It stays resumable from the run history."
        ),
        skey=f"hitl:{run_id_raw}",
    )
    decision.update({"action": "delivery", "delivery_tier": 2})
    return ("fired", "HITL question queued to the digest", decision)


async def sweep_hitl_aging(now: datetime | None = None) -> int:
    """Internal-event emitter (spec §17.2): HITL items pending past the
    ambient timeout produce an internal event the platform can react to."""
    from app.ambient.store import emit_event
    from app.registry_cache import get_cache

    now = now or datetime.now(UTC)
    timeout_h = int(await get_cache().setting("ambient_hitl_timeout_h"))
    cutoff = now - timedelta(hours=timeout_h)
    emitted = 0
    async with get_session_factory()() as session:
        stale = list(
            (
                await session.execute(
                    select(Run).where(
                        Run.status == "paused_hitl",
                        Run.started_at <= cutoff,
                        # ambient timeout applies to ambient runs only —
                        # interactive HITL keeps its own semantics (§7)
                        Run.trigger.isnot(None),
                    )
                )
            ).scalars()
        )
    for run in stale:
        event = await emit_event(
            kind="hitl_aged",
            source="internal",
            payload={"run_id": str(run.id)},
            dedupe_key=f"hitl_aged:{run.id}",
        )
        if event is not None:
            emitted += 1
    return emitted
