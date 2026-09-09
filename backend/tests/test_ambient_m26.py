"""M26 — ambient completeness pack (spec §18.1): per-routine model,
near-due tightening, escalation budget, per-item anticipation, learner
threshold recovery, multi-time digest shifting, judge token accounting."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ambient.deliver import add_delivery, flush_deliveries, record_feedback
from app.ambient.execute import execute_fired_event, prepare_run
from app.ambient.store import emit_event
from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import AmbientEvent, Delivery, Routine, StandingIntent
from app.settings_store import get_settings, update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _settings() -> dict[str, Any]:
    async with get_session_factory()() as session:
        return await get_settings(session)


async def _enable(**kv: Any) -> None:
    await _set(ambient_enabled=True, **kv)


async def _fired_routine_event(routine: Routine) -> AmbientEvent:
    event = await emit_event(kind="routine_fire", source="webhook", routine_id=routine.id)
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


# ── per-routine model_ref (spec §18.1) ───────────────────────────────


async def test_routine_model_ref_carried_in_trigger() -> None:
    await _enable()
    async with get_session_factory()() as session:
        routine = Routine(name=f"r-{uuid4().hex[:8]}", prompt="p", model_ref="fake:scripted")
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
    event = await _fired_routine_event(routine)
    run = await prepare_run(event)
    assert run is not None
    assert run.trigger is not None and run.trigger["model_ref"] == "fake:scripted"


async def test_routine_model_ref_overrides_default_model(seeded_client: Any) -> None:
    """The strong proof: the settings default points at an UNCONFIGURED
    provider; only the routine's model_ref override can let the run
    complete on the fake model."""
    await _enable()
    await _set(formatter_enabled=False, orchestrator_mode="graph")
    # raw row write: save-time validation (rightly) refuses an unconfigured
    # default, but a stored one must still lose to the routine's override
    async with get_session_factory()() as session:
        from app.models import AppSetting

        row = await session.get(AppSetting, "default_model")
        if row is None:
            session.add(
                AppSetting(key="default_model", value={"value": "anthropic:claude-sonnet-4-6"})
            )
        else:
            row.value = {"value": "anthropic:claude-sonnet-4-6"}
        await session.commit()
    from app.registry_cache import reset_cache

    reset_cache()
    async with get_session_factory()() as session:
        routine = Routine(name=f"r-{uuid4().hex[:8]}", prompt="p", model_ref="fake:scripted")
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
    event = await _fired_routine_event(routine)
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "PlannerOutput",
                "args": {"entries": [], "direct_answer": "done", "no_confident_match": False},
                "id": "m26-1",
            }
        ],
    )
    run_id = await asyncio.wait_for(execute_fired_event(event.id, poll_s=0.1), timeout=30)
    assert run_id is not None
    async with get_session_factory()() as session:
        from app.models import Run

        run = await session.get(Run, run_id)
        assert run is not None and run.status == "completed"


async def test_routine_api_validates_model_ref(seeded_client: Any) -> None:
    await _enable()
    bad = await seeded_client.post(
        "/api/v1/routines",
        json={"name": "bad-model", "prompt": "p", "model_ref": "nope:not-a-provider"},
    )
    assert bad.status_code == 422
    ok = await seeded_client.post(
        "/api/v1/routines",
        json={"name": "good-model", "prompt": "p", "model_ref": "fake:scripted"},
    )
    assert ok.status_code == 201


# ── near-due poller tightening (spec §18.1) ──────────────────────────


async def test_near_due_intent_polls_at_base_interval() -> None:
    from app.ambient.triggers import poll_due_intents, register_poll_source

    await _enable()
    now = datetime.now(UTC)
    polled: list[str] = []

    async def source(
        watermark: str | None, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        polled.append("hit")
        return [], watermark

    register_poll_source("m26src", source)
    try:
        async with get_session_factory()() as session:
            near = StandingIntent(
                text="near-due",
                condition_type="event",
                compiled={"poll": {"source": "m26src"}},
                base_interval_s=300,
                current_interval_s=3600,  # AIMD had backed off
                last_checked_at=now - timedelta(seconds=400),  # past base, not current
                expires_at=now + timedelta(seconds=1000),  # within 2× current
            )
            far = StandingIntent(
                text="far-out",
                condition_type="event",
                compiled={"poll": {"source": "m26src"}},
                base_interval_s=300,
                current_interval_s=3600,
                last_checked_at=now - timedelta(seconds=400),
                expires_at=now + timedelta(days=30),
            )
            session.add_all([near, far])
            await session.commit()
        await poll_due_intents(now=now)
        assert len(polled) == 1  # only the near-due one tightened to base
    finally:
        register_poll_source("m26src", None)


# ── escalation budget on digest approvals (spec §18.1) ───────────────


async def test_escalation_budget_caps_approval_flush(client: Any) -> None:
    await _enable(
        ambient_escalation_budget_per_day=1,
        ambient_digest_times=["00:01"],
        ambient_quiet_hours=["23:58", "23:59"],
    )
    normal = await add_delivery(category="routine", tier=2, urgency=2, title="normal item")
    low = await add_delivery(category="hitl", tier=2, urgency=2, title="approval low")
    high = await add_delivery(category="hitl", tier=2, urgency=4, title="approval high")
    other = await add_delivery(category="learning", tier=2, urgency=3, title="approval mid")
    now = datetime.now(UTC).replace(hour=12, minute=0)
    await flush_deliveries(now=now)
    async with get_session_factory()() as session:
        rows = {r.id: r for r in (await session.execute(select(Delivery))).scalars()}
    assert rows[normal.id].delivered_at is not None  # non-approvals unaffected
    assert rows[high.id].delivered_at is not None  # highest risk first
    assert rows[other.id].delivered_at is None  # over budget — next digest
    assert rows[low.id].delivered_at is None


# ── per-item anticipation deliveries (spec §18.1) ────────────────────


async def test_anticipation_one_delivery_per_item(seeded_client: Any) -> None:
    from app.ambient.anticipate import run_anticipation
    from app.models import Conversation, Run

    await _enable()
    await _set(default_model="fake:scripted")
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        session.add(
            Run(
                conversation_id=conv.id,
                chat_message="tune pgvector",
                status="completed",
                orchestrator_mode="graph",
                final_answer="use HNSW",
            )
        )
        await session.commit()
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "a1",
                "name": "AnticipationOutput",
                "args": {
                    "items": [
                        {"title": "HNSW params", "note": "ef trade-offs"},
                        {"title": "Index rebuild plan", "note": "concurrent build"},
                    ]
                },
            }
        ],
    )
    created = await run_anticipation()
    assert created is not None and len(created) == 2
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(select(Delivery).where(Delivery.category == "anticipation"))
            ).scalars()
        )
    assert len(rows) == 2
    assert all(r.tier == 2 for r in rows)
    # window dedupe still holds across the whole item set
    assert await run_anticipation() is None


# ── learner threshold recovery (spec §18.1) ──────────────────────────


async def test_learner_lowers_min_urgency_on_recovery(client: Any) -> None:
    from app.ambient.learn import run_learner

    await _enable(ambient_learning_mode="auto")
    async with get_session_factory()() as session:
        intent = StandingIntent(
            text="w", condition_type="event", status="active", budget={"min_urgency": 4}
        )
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
        intent_id = intent.id
    for i in range(5):
        d = await add_delivery(
            category=f"watchcat{i}", tier=2, urgency=3, title=f"w {i}", intent_id=intent_id
        )
        await record_feedback(d.id, "accepted")
    await run_learner(force=True)
    async with get_session_factory()() as session:
        fresh = await session.get(StandingIntent, intent_id)
        assert fresh is not None
        assert (fresh.budget or {}).get("min_urgency") == 3  # one step down
    # never below the default floor of 2
    async with get_session_factory()() as session:
        fresh = await session.get(StandingIntent, intent_id)
        assert fresh is not None
        fresh.budget = {"min_urgency": 2}
        await session.commit()
    await run_learner(force=True)
    async with get_session_factory()() as session:
        fresh = await session.get(StandingIntent, intent_id)
        assert fresh is not None
        assert (fresh.budget or {}).get("min_urgency") == 2


# ── multi-time digest shifting (spec §18.1) ──────────────────────────


async def test_digest_shift_moves_each_time_independently(client: Any) -> None:
    from app.ambient.learn import run_learner

    await _enable(ambient_learning_mode="auto", ambient_digest_times=["09:00", "17:00"])
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        for i in range(3):  # morning cluster at 07:30 → nearest 09:00
            session.add(
                Delivery(
                    category="am",
                    tier=2,
                    urgency=2,
                    title=f"am {i}",
                    channel="digest",
                    delivered_at=now.replace(hour=7, minute=30) - timedelta(days=i),
                    feedback="accepted",
                    reward=1.0,
                )
            )
        for i in range(3):  # evening cluster at 20:00 → nearest 17:00, clamped
            session.add(
                Delivery(
                    category="pm",
                    tier=2,
                    urgency=2,
                    title=f"pm {i}",
                    channel="digest",
                    delivered_at=now.replace(hour=20, minute=0) - timedelta(days=i),
                    feedback="accepted",
                    reward=1.0,
                )
            )
        await session.commit()
    await run_learner(force=True)
    got = (await _settings())["ambient_digest_times"]
    assert got == ["07:30", "19:00"]  # each time moved to ITS cluster, clamped ±2h


# ── judge token accounting (spec §18.1) ──────────────────────────────


async def test_judge_usage_hook_receives_tokens(client: Any) -> None:
    from app.ambient.decide import process_event, register_judge_usage_hook

    await _enable()
    await _set(default_model="fake:scripted")
    seen: list[dict[str, Any]] = []
    register_judge_usage_hook(seen.append)
    try:
        async with get_session_factory()() as session:
            session.add(
                StandingIntent(
                    text="watch",
                    condition_type="event",
                    status="active",
                    compiled={
                        "match": "events",
                        "filters": [{"field": "kind", "op": "equals", "value": "thing"}],
                    },
                    semantic_predicate="significant?",
                )
            )
            await session.commit()
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "id": "j1",
                    "name": "SignificanceOutput",
                    "args": {"significant": True, "urgency": 4, "reason": "urgent_change"},
                }
            ],
        )
        event = await emit_event(kind="thing", source="internal", payload={"x": 1})
        assert event is not None
        verdict, _reason, _decision = await process_event(event)
        assert verdict == "fired"  # include_raw path still parses correctly
        assert len(seen) == 1
        assert seen[0].get("input_tokens", 0) > 0  # fake provider stamps usage
    finally:
        register_judge_usage_hook(None)
