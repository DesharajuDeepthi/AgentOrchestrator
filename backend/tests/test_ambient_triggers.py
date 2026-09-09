"""Trigger + decision plane tests (spec §17.2/§17.3/§17.3a — milestone M21)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.ambient.decide import match_filters, process_event, sweep_hitl_aging
from app.ambient.patterns import advance_patterns, expire_pattern_deadlines
from app.ambient.store import emit_event
from app.ambient.triggers import (
    evaluate_schedules,
    evaluate_state_conditions,
    poll_due_intents,
    register_poll_source,
    register_state_probe,
)
from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import AmbientEvent, Routine, Run, StandingIntent
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable(**extra: Any) -> None:
    await _set(ambient_enabled=True, **extra)


async def _routine(**kw: Any) -> Routine:
    async with get_session_factory()() as session:
        row = Routine(name=kw.pop("name", f"r-{datetime.now(UTC).timestamp()}"), prompt="p", **kw)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _intent(**kw: Any) -> StandingIntent:
    async with get_session_factory()() as session:
        row = StandingIntent(
            text=kw.pop("text", "watch something"),
            condition_type=kw.pop("condition_type", "event"),
            **kw,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _events(kind: str | None = None) -> list[AmbientEvent]:
    async with get_session_factory()() as session:
        stmt = select(AmbientEvent)
        if kind:
            stmt = stmt.where(AmbientEvent.kind == kind)
        return list((await session.execute(stmt.order_by(AmbientEvent.received_at))).scalars())


# ── schedules ────────────────────────────────────────────────────────


async def test_interval_schedule_fires_and_respects_interval() -> None:
    await _enable()
    routine = await _routine(name="every-5m", triggers=[{"type": "interval", "seconds": 300}])
    now = datetime.now(UTC)
    assert await evaluate_schedules(now) == 1  # never fired ⇒ due
    assert await evaluate_schedules(now + timedelta(seconds=60)) == 0  # not yet
    assert await evaluate_schedules(now + timedelta(seconds=301)) == 1
    events = await _events("routine_schedule")
    assert len(events) == 2 and all(e.routine_id == routine.id for e in events)


async def test_once_schedule_fires_exactly_once_with_stagger() -> None:
    await _enable()
    at = datetime.now(UTC) + timedelta(minutes=5)
    routine = await _routine(
        name="one-shot", triggers=[{"type": "once", "at": at.isoformat()}], stagger_offset_s=60
    )
    assert await evaluate_schedules(at) == 0  # stagger not elapsed
    assert await evaluate_schedules(at + timedelta(seconds=61)) == 1
    assert await evaluate_schedules(at + timedelta(minutes=10)) == 0  # auto-disabled
    async with get_session_factory()() as session:
        row = await session.get(Routine, routine.id)
    assert row is not None and row.triggers[0]["type"] == "once_fired"


async def test_cron_schedule_fires_once_on_recovery_not_n_times() -> None:
    await _enable()
    await _routine(name="hourly", triggers=[{"type": "cron", "cron": "0 * * * *"}])
    # simulate a long outage: many missed slots, single catch-up fire
    later = datetime.now(UTC) + timedelta(days=1, minutes=1)
    assert await evaluate_schedules(later) == 1


async def test_paused_routine_never_schedules() -> None:
    await _enable()
    await _routine(name="paused", status="paused", triggers=[{"type": "interval", "seconds": 60}])
    assert await evaluate_schedules() == 0


# ── adaptive pollers ─────────────────────────────────────────────────


async def test_poller_aimd_backoff_and_reset() -> None:
    await _enable()
    feed: list[list[dict[str, Any]]] = [[], [], [{"id": "a1", "text": "fresh item"}]]
    calls = {"n": 0}

    async def source(
        watermark: str | None, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        items = feed[min(calls["n"], len(feed) - 1)]
        calls["n"] += 1
        return items, "a1" if items else watermark

    register_poll_source("test-feed", source)
    try:
        intent = await _intent(
            compiled={"poll": {"source": "test-feed"}},
            base_interval_s=300,
            current_interval_s=300,
            backoff_multiplier=2.0,
            max_interval_s=1200,
        )
        t0 = datetime.now(UTC)
        assert await poll_due_intents(t0) == 0  # quiet ⇒ backoff 300→600
        async with get_session_factory()() as session:
            row = await session.get(StandingIntent, intent.id)
            assert row is not None and row.current_interval_s == 600
        assert await poll_due_intents(t0 + timedelta(seconds=300)) == 0  # not due yet
        assert await poll_due_intents(t0 + timedelta(seconds=601)) == 0  # quiet ⇒ 1200
        assert await poll_due_intents(t0 + timedelta(seconds=1802)) == 1  # hit ⇒ reset
        async with get_session_factory()() as session:
            row = await session.get(StandingIntent, intent.id)
            assert row is not None
            assert row.current_interval_s == 300 and row.watermark == "a1"
        assert (await _events("intent_poll_item"))[0].payload == {
            "item": {"id": "a1", "text": "fresh item"}
        }
    finally:
        register_poll_source("test-feed", None)


async def test_poller_expires_intent() -> None:
    await _enable()

    async def source(
        w: str | None, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        return [], w

    register_poll_source("exp-feed", source)
    try:
        intent = await _intent(
            compiled={"poll": {"source": "exp-feed"}},
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        await poll_due_intents()
        async with get_session_factory()() as session:
            row = await session.get(StandingIntent, intent.id)
        assert row is not None and row.status == "expired"
    finally:
        register_poll_source("exp-feed", None)


# ── state conditions (edge, not level) ───────────────────────────────


async def test_state_condition_fires_on_edge_only() -> None:
    await _enable()
    value = {"v": 0.0}

    async def probe(config: dict[str, Any]) -> float:
        return value["v"]

    register_state_probe("test-gauge", probe)
    try:
        await _intent(
            condition_type="state",
            compiled={"probe": "test-gauge", "op": ">=", "value": 3},
        )
        assert await evaluate_state_conditions() == 0  # below threshold
        value["v"] = 5.0
        assert await evaluate_state_conditions() == 1  # FALSE→TRUE edge
        assert await evaluate_state_conditions() == 0  # still holding: no refire
        value["v"] = 1.0
        assert await evaluate_state_conditions() == 0  # cleared
        value["v"] = 4.0
        assert await evaluate_state_conditions() == 1  # new edge
    finally:
        register_state_probe("test-gauge", None)


# ── decision plane ───────────────────────────────────────────────────


def test_match_filters_operators() -> None:
    payload = {"pr": {"title": "fix: crash on save", "labels": "bug,urgent"}, "n": 7}
    assert match_filters(payload, [{"field": "pr.title", "op": "starts_with", "value": "fix:"}])
    assert match_filters(payload, [{"field": "pr.labels", "op": "contains", "value": "urgent"}])
    assert match_filters(payload, [{"field": "n", "op": "one_of", "values": [7, 9]}])
    assert match_filters(payload, [{"field": "pr.title", "op": "regex", "value": r"crash|panic"}])
    assert not match_filters(payload, [{"field": "pr.title", "op": "equals", "value": "other"}])
    assert not match_filters(payload, [{"field": "missing.path", "op": "contains", "value": "x"}])


async def test_decide_routine_event_fires_and_writes_ledger() -> None:
    await _enable()
    routine = await _routine(name="fire-me")
    event = await emit_event(kind="routine_fire", source="webhook", routine_id=routine.id)
    assert event is not None
    verdict, reason, decision = await process_event(event)
    assert (verdict, reason) == ("fired", "routine trigger matched")
    assert decision == {"tier": 1, "urgency": 2, "fired_for": "routine"}


async def test_decide_tier1_filters_hold() -> None:
    await _enable()
    routine = await _routine(name="filtered")
    event = await emit_event(
        kind="routine_schedule",
        source="schedule",
        routine_id=routine.id,
        payload={
            "trigger": {"filters": [{"field": "branch", "op": "equals", "value": "main"}]},
            "payload": {"branch": "dev"},
        },
    )
    assert event is not None
    assert (await process_event(event))[:2] == ("held", "tier-1 filters did not match")


async def test_decide_runs_per_day_cap_drops() -> None:
    await _enable(ambient_runs_per_day=1)
    routine = await _routine(name="capped")
    # one ambient run already happened today — the cap of 1 is spent
    from app.orchestrator.runner import create_run

    prior = await create_run(None, "earlier ambient work")
    async with get_session_factory()() as session:
        await session.execute(
            update(Run)
            .where(Run.id == prior.id)
            .values(status="completed", trigger={"routine_id": str(routine.id)})
        )
        await session.commit()
    event = await emit_event(kind="routine_fire", source="manual", routine_id=routine.id)
    assert event is not None
    verdict = await process_event(event)
    assert verdict is not None and verdict[0] == "dropped" and "cap" in verdict[1]


async def test_decide_paused_routine_drops() -> None:
    await _enable()
    routine = await _routine(name="asleep", status="paused")
    event = await emit_event(kind="routine_fire", source="manual", routine_id=routine.id)
    assert event is not None
    verdict = await process_event(event)
    assert verdict is not None and verdict[0] == "dropped"


async def test_significance_judge_gates_intent_events() -> None:
    await _enable()
    await _set(default_model="fake:scripted")
    intent = await _intent(semantic_predicate="only genuinely urgent production issues")
    # significant ⇒ fired, with urgency in the ledger
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "c1",
                "name": "SignificanceOutput",
                "args": {"significant": True, "urgency": 4, "reason": "urgent_change"},
            }
        ],
    )
    e1 = await emit_event(kind="intent_poll_item", source="poll", intent_id=intent.id)
    assert e1 is not None
    v1 = await process_event(e1)
    assert v1[:2] == ("fired", "intent condition met")
    assert v1[2]["tier"] == 2 and v1[2]["urgency"] == 4
    # not significant ⇒ held
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "c2",
                "name": "SignificanceOutput",
                "args": {"significant": False, "urgency": 1, "reason": "noise"},
            }
        ],
    )
    e2 = await emit_event(kind="intent_poll_item", source="poll", intent_id=intent.id)
    assert e2 is not None
    assert (await process_event(e2))[:2] == ("held", "judged not significant")


async def test_significance_judge_failure_holds() -> None:
    await _enable()
    await _set(default_model="fake:scripted")
    intent = await _intent(semantic_predicate="anything")
    fake_llm.push_ai("not a structured output at all")
    event = await emit_event(kind="intent_poll_item", source="poll", intent_id=intent.id)
    assert event is not None
    verdict = await process_event(event)
    assert verdict is not None and verdict[0] == "held" and "judge failed" in verdict[1]


async def test_presence_events_hold_as_informational() -> None:
    await _enable()
    event = await emit_event(kind="user_returned", source="presence")
    assert event is not None
    verdict = await process_event(event)
    assert verdict is not None and verdict[0] == "held"


# ── CEP-lite patterns (spec §17.3a) ──────────────────────────────────


def _pattern_intent_spec(kind: str, window_s: int = 3600) -> dict[str, Any]:
    return {
        "pattern": {
            "kind": kind,
            "a": {"filters": [{"field": "step", "op": "equals", "value": "A"}]},
            "b": {"filters": [{"field": "step", "op": "equals", "value": "B"}]},
            "window_s": window_s,
            "cooldown_s": 0,
        }
    }


async def test_pattern_filters_see_kind_and_source() -> None:
    """Filters share one data model across the decision plane: kind, source,
    payload fields (M24 harness regression — patterns saw payload only)."""
    await _enable()
    await _intent(
        compiled={
            "pattern": {
                "kind": "sequence",
                "a": {"filters": [{"field": "kind", "op": "equals", "value": "deploy_done"}]},
                "b": {"filters": [{"field": "kind", "op": "equals", "value": "alert_raised"}]},
                "window_s": 300,
                "cooldown_s": 0,
            }
        }
    )
    a = await emit_event(kind="deploy_done", source="internal", payload={"rev": "2ea11"})
    assert a is not None
    await advance_patterns(a)
    b = await emit_event(kind="alert_raised", source="internal")
    assert b is not None
    assert await advance_patterns(b) == 1


async def test_sequence_pattern_matches_within_window() -> None:
    await _enable()
    intent = await _intent(compiled=_pattern_intent_spec("sequence"))
    a = await emit_event(kind="x", source="manual", payload={"step": "A"})
    assert a is not None
    await advance_patterns(a)
    b = await emit_event(kind="x", source="manual", payload={"step": "B"})
    assert b is not None
    assert await advance_patterns(b) == 1
    matched = await _events("pattern_matched")
    assert len(matched) == 1
    assert matched[0].intent_id == intent.id
    assert matched[0].causation_id == b.id and matched[0].depth == 1


async def test_absence_pattern_fires_on_deadline_not_before() -> None:
    await _enable()
    await _intent(compiled=_pattern_intent_spec("absence", window_s=60))
    a = await emit_event(kind="x", source="manual", payload={"step": "A"})
    assert a is not None
    await advance_patterns(a)
    assert await expire_pattern_deadlines(datetime.now(UTC)) == 0  # not due
    fired = await expire_pattern_deadlines(datetime.now(UTC) + timedelta(seconds=61))
    assert fired == 1
    absence = await _events("pattern_absence")
    assert len(absence) == 1 and absence[0].causation_id == a.id


async def test_absence_pattern_satisfied_by_b_never_fires() -> None:
    await _enable()
    await _intent(compiled=_pattern_intent_spec("absence", window_s=60))
    a = await emit_event(kind="x", source="manual", payload={"step": "A"})
    assert a is not None
    await advance_patterns(a)
    b = await emit_event(kind="x", source="manual", payload={"step": "B"})
    assert b is not None
    await advance_patterns(b)
    assert await expire_pattern_deadlines(datetime.now(UTC) + timedelta(seconds=61)) == 0
    assert await _events("pattern_absence") == []


async def test_conjunction_pattern_matches_either_order() -> None:
    await _enable()
    await _intent(compiled=_pattern_intent_spec("conjunction"))
    b = await emit_event(kind="x", source="manual", payload={"step": "B"})
    assert b is not None
    await advance_patterns(b)
    a = await emit_event(kind="x", source="manual", payload={"step": "A"})
    assert a is not None
    assert await advance_patterns(a) == 1


async def test_pattern_derived_events_respect_depth_guard() -> None:
    """A pattern match caused by an already-deep event must be guarded."""
    await _enable()
    await _intent(compiled=_pattern_intent_spec("sequence"))
    root = await emit_event(kind="r0", source="manual")
    assert root is not None
    deep = root
    for i in range(1, 3):
        nxt = await emit_event(kind=f"r{i}", source="pattern", caused_by=deep)
        assert nxt is not None
        deep = nxt
    a = await emit_event(kind="x", source="pattern", payload={"step": "A"}, caused_by=deep)
    assert a is not None and a.depth == 3
    await advance_patterns(a)
    b = await emit_event(kind="x", source="manual", payload={"step": "B"})
    assert b is not None
    await advance_patterns(b)  # match completes on B (depth 0) — allowed
    # but a match caused by the deep chain is blocked at depth 4:
    emitted = await _events("pattern_matched")
    assert len(emitted) == 1  # via B, not via the deep chain


# ── HITL aging internal event ────────────────────────────────────────


async def test_drain_with_default_processor_no_deadlock() -> None:
    """Regression (found live): the drain holds FOR UPDATE locks while the
    processor decides — the processor must never write ambient_events itself.
    A hang here means the self-deadlock is back."""
    import asyncio

    from app.ambient.drain import default_processor, drain_once, register_processor

    await _enable()
    routine = await _routine(name="deadlock-check")
    event = await emit_event(kind="routine_fire", source="manual", routine_id=routine.id)
    assert event is not None
    register_processor(default_processor)
    try:
        handled = await asyncio.wait_for(drain_once(), timeout=10)
    finally:
        register_processor(None)
    assert handled == 1
    async with get_session_factory()() as session:
        row = await session.get(AmbientEvent, event.id)
    assert row is not None and row.verdict == "fired"
    assert row.decision == {"tier": 1, "urgency": 2, "fired_for": "routine"}


async def test_hitl_aging_sweep_emits_once() -> None:
    await _enable(ambient_hitl_timeout_h=1)
    from app.orchestrator.runner import create_run

    run = await create_run(None, "needs approval")
    async with get_session_factory()() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run.id)
            .values(
                status="paused_hitl",
                started_at=datetime.now(UTC) - timedelta(hours=2),
                # M22: the ambient timeout applies to ambient runs only
                trigger={"routine_id": None, "source": "manual"},
            )
        )
        await session.commit()
    assert await sweep_hitl_aging() == 1
    assert await sweep_hitl_aging() == 0  # deduped
    events = await _events("hitl_aged")
    assert len(events) == 1 and events[0].payload == {"run_id": str(run.id)}
