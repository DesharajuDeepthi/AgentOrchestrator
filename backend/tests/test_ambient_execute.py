"""Execution-plane tests (spec §17.4 — milestone M22): fires become runs,
agent wakeups (H2), the liveness watchdog (H3), HITL timeout semantics, and
the narrowed registry projection."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ambient.drain import drain_once, register_executor, register_processor
from app.ambient.execute import (
    DEFAULT_BUDGETS,
    _supervise,
    execute_fired_event,
    finish_ambient_run,
    prepare_run,
    reap_stalled_runs,
)
from app.ambient.store import emit_event
from app.ambient.wakeups import (
    WAKEUP_MAX_DELAY_S,
    WAKEUP_MAX_PENDING,
    WAKEUP_MIN_DELAY_S,
    WakeupCapError,
    cancel_wakeup,
    fire_due_wakeups,
    schedule_wakeup,
)
from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import (
    AmbientEvent,
    AmbientWakeup,
    Conversation,
    Delivery,
    Routine,
    Run,
    StandingIntent,
)
from app.orchestrator.context import RunContext, set_run_context
from app.orchestrator.runner import RUNNING_TASKS
from app.retrieval import apply_ambient_allowlist
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable() -> None:
    await _set(ambient_enabled=True)


async def _make_routine(**kw: Any) -> Routine:
    async with get_session_factory()() as session:
        routine = Routine(
            name=kw.pop("name", f"r-{uuid4().hex[:8]}"), prompt="check the thing", **kw
        )
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
        return routine


async def _fired_routine_event(routine: Routine, **payload: Any) -> AmbientEvent:
    event = await emit_event(
        kind="routine_schedule", source="schedule", payload=payload or None, routine_id=routine.id
    )
    assert event is not None
    async with get_session_factory()() as session:
        row = await session.get(AmbientEvent, event.id)
        assert row is not None
        row.verdict = "fired"
        row.verdict_reason = "test"
        row.decision = {"tier": 1, "urgency": 2, "fired_for": "routine"}
        await session.commit()
        await session.refresh(row)
        return row


def _ctx(run_id: Any = None, allowlist: dict[str, Any] | None = None) -> RunContext:
    ctx = RunContext(
        run_id=run_id or uuid4(), mode="graph", recorder=None, ambient_allowlist=allowlist
    )
    set_run_context(ctx)
    return ctx


# ── narrowed registry projection (spec §17.4) ────────────────────────


async def test_allowlist_filters_by_name_and_id() -> None:
    records = [
        {"id": "1", "name": "alpha"},
        {"id": "2", "name": "beta", "tool_key": "native:beta"},
        {"id": "3", "name": "gamma"},
    ]
    _ctx(allowlist={"tools": ["alpha", "2"]})
    got = apply_ambient_allowlist(records, kind="tools")
    assert [r["name"] for r in got] == ["alpha", "beta"]
    # a kind the allowlist does not mention stays unrestricted
    assert apply_ambient_allowlist(records, kind="skills") == records


async def test_allowlist_absent_is_identity() -> None:
    records = [{"id": "1", "name": "alpha"}]
    _ctx(allowlist=None)
    assert apply_ambient_allowlist(records, kind="tools") == records


# ── fires become ordinary runs (spec §17.4) ──────────────────────────


async def test_prepare_run_carries_provenance_and_fenced_payload() -> None:
    await _enable()
    routine = await _make_routine(autonomy="propose")
    event = await _fired_routine_event(routine, note="conveyor stopped")
    run = await prepare_run(event)
    assert run is not None
    assert run.trigger is not None
    assert run.trigger["routine_id"] == str(routine.id)
    assert run.trigger["event_id"] == str(event.id)
    assert run.trigger["source"] == "schedule"
    # trusted routine prompt + untrusted-fenced payload + abstain instruction
    assert "check the thing" in run.chat_message
    assert "<untrusted_event_payload>" in run.chat_message
    assert "conveyor stopped" in run.chat_message
    assert "ABSTAIN" in run.chat_message
    assert "propose" in run.chat_message
    async with get_session_factory()() as session:
        conv = await session.get(Conversation, run.conversation_id)
        assert conv is not None and conv.title.startswith("[ambient]")


async def test_prepare_run_skips_paused_routine() -> None:
    await _enable()
    routine = await _make_routine(status="paused")
    event = await _fired_routine_event(routine)
    assert await prepare_run(event) is None


async def test_finish_completed_writes_digest_delivery_and_resets_failures() -> None:
    await _enable()
    routine = await _make_routine(consecutive_failures=2)
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "completed"
        row.final_answer = "Line 3 stopped; maintenance notified."
        await session.commit()
    await finish_ambient_run(run.id, event.id)
    async with get_session_factory()() as session:
        delivery = (await session.execute(select(Delivery))).scalars().one()
        assert delivery.tier == 2  # digest is the default (spec §17.5)
        assert delivery.run_id == run.id
        assert "Line 3 stopped" in (delivery.body or "")
        fresh = await session.get(Routine, routine.id)
        assert fresh is not None and fresh.consecutive_failures == 0


async def test_finish_abstained_is_silent_tier3() -> None:
    await _enable()
    routine = await _make_routine()
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "completed"
        row.final_answer = "ABSTAIN: nothing changed since the last check"
        await session.commit()
    await finish_ambient_run(run.id, event.id)
    async with get_session_factory()() as session:
        delivery = (await session.execute(select(Delivery))).scalars().one()
        assert delivery.tier == 3  # silence is an explicit, logged decision
        assert delivery.category == "abstained"


async def test_finish_failed_counts_up_self_wakes_and_pauses_at_three() -> None:
    await _enable()
    routine = await _make_routine(consecutive_failures=2)
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "failed"
        row.error = "tool exploded"
        await session.commit()
    await finish_ambient_run(run.id, event.id)
    async with get_session_factory()() as session:
        fresh = await session.get(Routine, routine.id)
        assert fresh is not None
        assert fresh.consecutive_failures == 3
        assert fresh.status == "paused"  # §17.6 auto-pause with visible reason
        assert "3 consecutive failures" in (fresh.status_reason or "")
        # Letta pattern: one self-wake with the error in context
        wake = (await session.execute(select(AmbientWakeup))).scalars().one()
        assert wake.created_by == "system"
        assert wake.run_id == run.id
        assert "tool exploded" in wake.reason
        deliveries = list((await session.execute(select(Delivery))).scalars())
        assert any(d.tier == 0 for d in deliveries)  # hard failure: auto-pause


async def test_finish_failed_self_wake_deduped_per_run() -> None:
    await _enable()
    routine = await _make_routine()
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "failed"
        row.error = "boom"
        await session.commit()
    await finish_ambient_run(run.id, event.id)
    await finish_ambient_run(run.id, event.id)
    async with get_session_factory()() as session:
        wakes = list((await session.execute(select(AmbientWakeup))).scalars())
        assert len(wakes) == 1


async def test_runs_per_day_budget_not_charged_for_held(client: Any) -> None:
    # the fired verdict is the only path that creates runs; drain with no
    # executor registered must never create one
    await _enable()
    register_processor(None)
    register_executor(None)
    await emit_event(kind="x", source="manual")
    await drain_once()
    async with get_session_factory()() as session:
        runs = list((await session.execute(select(Run))).scalars())
        assert runs == []


async def test_drain_hands_fired_events_to_executor() -> None:
    await _enable()
    routine = await _make_routine()
    seen: list[Any] = []

    async def fake_exec(event_id: Any) -> None:
        seen.append(event_id)

    async def fire_all(event: AmbientEvent) -> tuple[str, str, dict[str, Any]]:
        return ("fired", "test", {"fired_for": "routine"})

    register_processor(fire_all)
    register_executor(fake_exec)
    try:
        event = await emit_event(kind="k", source="manual", routine_id=routine.id)
        assert event is not None
        await drain_once()
        await asyncio.sleep(0.05)  # executor runs as a fire-and-forget task
        assert seen == [event.id]
    finally:
        register_processor(None)
        register_executor(None)


# ── budgets + tokens-without-progress (spec §17.4) ───────────────────


async def test_supervisor_heartbeats_and_cancels_on_wall_clock() -> None:
    await _enable()
    routine = await _make_routine()
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    blocker = asyncio.Event()

    async def _hang() -> None:
        await blocker.wait()

    task = asyncio.create_task(_hang())
    RUNNING_TASKS[run.id] = task
    try:
        budgets = dict(DEFAULT_BUDGETS, wall_clock_s=0)
        status = await asyncio.wait_for(_supervise(run.id, budgets, poll_s=0.05), timeout=10)
        assert status == "cancelled"
        async with get_session_factory()() as session:
            row = await session.get(Run, run.id)
            assert row is not None
            assert row.status == "cancelled"
            assert "wall_clock" in (row.error or "")
            assert row.last_heartbeat_at is not None  # H3: heartbeat refreshed
    finally:
        blocker.set()
        RUNNING_TASKS.pop(run.id, None)


async def test_supervisor_cancels_on_token_budget() -> None:
    await _enable()
    routine = await _make_routine(budgets={"max_tokens": 10})
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.total_output_tokens = 50
        await session.commit()
    status = await asyncio.wait_for(
        _supervise(run.id, dict(DEFAULT_BUDGETS, max_tokens=10), poll_s=0.05), timeout=10
    )
    assert status == "cancelled"
    async with get_session_factory()() as session:
        fresh = await session.get(Run, run.id)
        assert fresh is not None and "max_tokens" in (fresh.error or "")


async def test_supervisor_returns_terminal_status() -> None:
    await _enable()
    routine = await _make_routine()
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "completed"
        await session.commit()
    status = await asyncio.wait_for(_supervise(run.id, DEFAULT_BUDGETS, poll_s=0.05), timeout=10)
    assert status == "completed"


# ── agent wakeups H2 (spec §17.4): clamps, caps, done-guard ──────────


async def test_wakeup_delay_clamped_to_window() -> None:
    await _enable()
    routine = await _make_routine()
    low = await schedule_wakeup(routine.id, delay_s=5, reason="too soon")
    high = await schedule_wakeup(routine.id, delay_s=999_999, reason="too late")
    now = datetime.now(UTC)
    assert (low.due_at - now).total_seconds() >= WAKEUP_MIN_DELAY_S - 2
    assert (high.due_at - now).total_seconds() <= WAKEUP_MAX_DELAY_S + 2


async def test_wakeup_pending_cap() -> None:
    await _enable()
    routine = await _make_routine()
    for i in range(WAKEUP_MAX_PENDING):
        await schedule_wakeup(routine.id, delay_s=3600 + i, reason=f"w{i}")
    with pytest.raises(WakeupCapError, match="pending"):
        await schedule_wakeup(routine.id, delay_s=7200, reason="one too many")


async def test_wakeup_daily_cap() -> None:
    await _enable()
    await _set(ambient_wakeups_per_routine_per_day=2)
    routine = await _make_routine()
    a = await schedule_wakeup(routine.id, delay_s=3600, reason="a")
    b = await schedule_wakeup(routine.id, delay_s=3600, reason="b")
    assert await cancel_wakeup(a.id) and await cancel_wakeup(b.id)  # cancelled still count
    with pytest.raises(WakeupCapError, match="per day"):
        await schedule_wakeup(routine.id, delay_s=3600, reason="c")


async def test_cancel_wakeup_only_pending() -> None:
    await _enable()
    routine = await _make_routine()
    w = await schedule_wakeup(routine.id, delay_s=3600, reason="x")
    assert await cancel_wakeup(w.id) is True
    assert await cancel_wakeup(w.id) is False  # already cancelled


async def test_fire_due_wakeups_emits_routine_event() -> None:
    await _enable()
    routine = await _make_routine()
    w = await schedule_wakeup(routine.id, delay_s=60, reason="follow up on the ticket")
    future = datetime.now(UTC) + timedelta(hours=1)
    fired = await fire_due_wakeups(now=future)
    assert fired == 1
    async with get_session_factory()() as session:
        fresh = await session.get(AmbientWakeup, w.id)
        assert fresh is not None and fresh.status == "fired"
        event = (
            (await session.execute(select(AmbientEvent).where(AmbientEvent.source == "wakeup")))
            .scalars()
            .one()
        )
        assert event.routine_id == routine.id
        assert "follow up on the ticket" in str(event.payload)


async def test_wakeup_done_guard_expires_superseded() -> None:
    await _enable()
    routine = await _make_routine()
    w = await schedule_wakeup(routine.id, delay_s=60, reason="check again")
    # a run for this routine completed AFTER the wakeup was scheduled
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        run = Run(
            conversation_id=conv.id,
            chat_message="m",
            status="completed",
            orchestrator_mode="graph",
            trigger={"routine_id": str(routine.id)},
        )
        session.add(run)
        await session.commit()
    future = datetime.now(UTC) + timedelta(hours=1)
    fired = await fire_due_wakeups(now=future)
    assert fired == 0
    async with get_session_factory()() as session:
        fresh = await session.get(AmbientWakeup, w.id)
        assert fresh is not None and fresh.status == "expired"


async def test_wakeup_not_due_untouched() -> None:
    await _enable()
    routine = await _make_routine()
    w = await schedule_wakeup(routine.id, delay_s=3600, reason="later")
    assert await fire_due_wakeups() == 0
    async with get_session_factory()() as session:
        fresh = await session.get(AmbientWakeup, w.id)
        assert fresh is not None and fresh.status == "pending"


# ── liveness watchdog H3 (spec §17.4) ────────────────────────────────


async def test_reaper_stalls_run_and_pauses_routine() -> None:
    await _enable()
    routine = await _make_routine()
    stale = datetime.now(UTC) - timedelta(minutes=10)
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        run = Run(
            conversation_id=conv.id,
            chat_message="m",
            status="running",
            orchestrator_mode="graph",
            trigger={"routine_id": str(routine.id)},
            last_heartbeat_at=stale,
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    reaped = await reap_stalled_runs()
    assert reaped == 1
    async with get_session_factory()() as session:
        fresh = await session.get(Run, run_id)
        assert fresh is not None
        assert fresh.status == "stalled"
        assert "stalled" in (fresh.error or "")
        fresh_routine = await session.get(Routine, routine.id)
        assert fresh_routine is not None
        assert fresh_routine.status == "paused"
        assert str(run_id) in (fresh_routine.status_reason or "")


async def test_reaper_ignores_interactive_and_fresh_runs() -> None:
    await _enable()
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        # interactive run (no trigger) with a stale heartbeat: not ambient's
        session.add(
            Run(
                conversation_id=conv.id,
                chat_message="m",
                status="running",
                orchestrator_mode="graph",
                last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        # ambient run with a fresh heartbeat
        session.add(
            Run(
                conversation_id=conv.id,
                chat_message="m",
                status="running",
                orchestrator_mode="graph",
                trigger={"routine_id": str(uuid4())},
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        await session.commit()
    assert await reap_stalled_runs() == 0


# ── HITL timeout semantics (spec §17.4/§17.5) ────────────────────────


async def test_hitl_aged_ambient_run_queues_tier2_delivery() -> None:
    await _enable()
    from app.ambient.decide import process_event, sweep_hitl_aging

    routine = await _make_routine()
    old = datetime.now(UTC) - timedelta(hours=48)
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        run = Run(
            conversation_id=conv.id,
            chat_message="m",
            status="paused_hitl",
            orchestrator_mode="graph",
            trigger={"routine_id": str(routine.id)},
            started_at=old,
        )
        # an interactive paused run must NOT be swept by the ambient timeout
        session.add(run)
        session.add(
            Run(
                conversation_id=conv.id,
                chat_message="m",
                status="paused_hitl",
                orchestrator_mode="graph",
                started_at=old,
            )
        )
        await session.commit()
        run_id = run.id
    emitted = await sweep_hitl_aging()
    assert emitted == 1
    async with get_session_factory()() as session:
        event = (
            (await session.execute(select(AmbientEvent).where(AmbientEvent.kind == "hitl_aged")))
            .scalars()
            .one()
        )
    verdict, _reason, decision = await process_event(event)
    assert verdict == "fired" and decision.get("action") == "delivery"
    async with get_session_factory()() as session:
        delivery = (await session.execute(select(Delivery))).scalars().one()
        assert delivery.tier == 2  # the question rides the digest
        assert delivery.run_id == run_id
        # the checkpoint stays resumable: the run is still paused
        fresh = await session.get(Run, run_id)
        assert fresh is not None and fresh.status == "paused_hitl"


# ── standing-intent watch tools (spec §17.4 compile-echo-confirm) ────


async def test_watch_compiles_echoes_and_needs_confirm() -> None:
    await _enable()
    await _set(default_model="fake:scripted")
    from app.native.ambient_tools import ambient_confirm_watch, ambient_watch

    _ctx()
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "w1",
                "name": "WatchCompile",
                "args": {
                    "mode": "events",
                    "filters": [{"field": "kind", "op": "equals", "value": "hitl_aged"}],
                    "semantic_predicate": None,
                    "echo": "Watch for runs stuck waiting on your input.",
                },
            }
        ],
    )
    out = await ambient_watch("tell me when a run gets stuck waiting on me")
    assert out["status"] == "proposed"
    assert "stuck waiting" in out["interpretation"]
    intent_id = out["intent_id"]
    async with get_session_factory()() as session:
        row = await session.get(StandingIntent, intent_id)
        assert row is not None
        assert row.status == "proposed"  # never active without the confirm step
        assert (row.compiled or {}).get("match") == "events"
    # empty id confirms the most recently proposed watch (chat-turn UX:
    # the id lives in a prior run's tool result, not the history)
    confirmed = await ambient_confirm_watch()
    assert confirmed["status"] == "active"
    assert confirmed["intent_id"] == str(intent_id)


async def test_unwatch_retires_intent() -> None:
    await _enable()
    from app.native.ambient_tools import ambient_unwatch

    async with get_session_factory()() as session:
        intent = StandingIntent(text="watch x", condition_type="event", status="active")
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
    out = await ambient_unwatch(str(intent.id))
    assert out["retired"] is True
    async with get_session_factory()() as session:
        fresh = await session.get(StandingIntent, intent.id)
        assert fresh is not None and fresh.status == "retired"


async def test_wakeup_tool_requires_ambient_run_context() -> None:
    await _enable()
    from app.native.ambient_tools import ambient_wakeup

    _ctx()  # a context whose run has no ambient trigger
    out = await ambient_wakeup(reason="ping me")
    assert out["scheduled"] is False
    assert "ambient" in out["error"]


# ── event-matched watches (spec §17.3 via the decision plane) ────────


async def test_event_intent_matches_unaddressed_internal_event() -> None:
    await _enable()
    from app.ambient.decide import process_event

    async with get_session_factory()() as session:
        intent = StandingIntent(
            text="tell me when the disk fills up",
            condition_type="event",
            status="active",
            compiled={
                "match": "events",
                "filters": [{"field": "kind", "op": "equals", "value": "disk_full"}],
            },
        )
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
    event = await emit_event(kind="disk_full", source="internal", payload={"pct": 97})
    assert event is not None
    verdict, _reason, decision = await process_event(event)
    assert verdict == "fired"
    assert decision.get("fired_for") == "intent"
    assert decision.get("intent_id") == str(intent.id)
    # an event nothing watches still holds
    other = await emit_event(kind="disk_ok", source="internal")
    assert other is not None
    verdict2, _r2, _d2 = await process_event(other)
    assert verdict2 == "held"


# ── end-to-end on the fake provider ──────────────────────────────────


async def test_execute_fired_event_end_to_end(seeded_client: Any) -> None:
    await _enable()
    await _set(default_model="fake:scripted", formatter_enabled=False, orchestrator_mode="graph")
    routine = await _make_routine(budgets={"wall_clock_s": 120})
    event = await _fired_routine_event(routine, note="all quiet")
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "PlannerOutput",
                "args": {
                    "entries": [],
                    "direct_answer": "ABSTAIN: nothing to report",
                    "no_confident_match": False,
                },
                "id": "amb-e2e-1",
            }
        ],
    )
    run_id = await asyncio.wait_for(execute_fired_event(event.id, poll_s=0.1), timeout=30)
    assert run_id is not None
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert run.status == "completed"
        delivery = (await session.execute(select(Delivery))).scalars().one()
        assert delivery.tier == 3 and delivery.category == "abstained"
