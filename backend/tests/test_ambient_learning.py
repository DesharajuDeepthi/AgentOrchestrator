"""Adaptive policy learning tests (spec §17.7 — milestone M25): the bandit
learner over the M23 reward substrate. Auto mode is first-class (applies
immediately under hard clamps, ledgered, revertible); propose mode queues;
off collects only. Never into tier 0, digest times ≤ 2h from the anchor."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.ambient.deliver import add_delivery, current_tier_override, record_feedback
from app.ambient.learn import (
    LEARN_MIN_SAMPLE,
    apply_policy,
    run_learner,
)
from app.db import get_session_factory
from app.models import AmbientPolicy, Delivery, StandingIntent
from app.settings_store import get_settings, update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable(mode: str = "auto", **kv: Any) -> None:
    await _set(ambient_enabled=True, ambient_learning_mode=mode, **kv)


async def _judged(category: str, tier: int, feedback: str, n: int) -> None:
    for i in range(n):
        d = await add_delivery(category=category, tier=tier, urgency=3, title=f"{category} {i}")
        await record_feedback(d.id, feedback)


async def _policies(category: str | None = None) -> list[AmbientPolicy]:
    async with get_session_factory()() as session:
        query = select(AmbientPolicy).order_by(AmbientPolicy.created_at)
        if category:
            query = query.where(AmbientPolicy.category == category)
        return list((await session.execute(query)).scalars())


# ── auto mode: applies immediately, no approval (spec §17.7 primary) ─


async def test_auto_demotes_after_three_dismissals_without_approval(client: Any) -> None:
    await _enable("auto")
    await _judged("noisy-interrupts", 1, "dismissed", LEARN_MIN_SAMPLE)
    changed = await run_learner(force=True)
    assert changed >= 1
    assert await current_tier_override("noisy-interrupts") == 2  # one step down
    rows = await _policies("noisy-interrupts")
    assert rows and rows[-1].source == "learner"  # applied, no approval step
    # next delivery in the category lands demoted
    nxt = await add_delivery(category="noisy-interrupts", tier=1, urgency=3, title="post")
    async with get_session_factory()() as session:
        fresh = await session.get(Delivery, nxt.id)
        assert fresh is not None and fresh.tier == 2


async def test_auto_promotes_but_never_into_tier0(client: Any) -> None:
    await _enable("auto")
    await _judged("golden", 2, "accepted", 6)
    await run_learner(force=True)
    assert await current_tier_override("golden") == 1  # promoted one step
    # keep accepting — a second pass must NOT promote into tier 0
    await _judged("golden", 1, "accepted", 6)
    await run_learner(force=True)
    assert await current_tier_override("golden") == 1
    for p in await _policies("golden"):
        assert p.tier_override is None or p.tier_override >= 1


async def test_auto_one_step_at_a_time(client: Any) -> None:
    await _enable("auto")
    await _judged("very-noisy", 1, "dismissed", 8)
    await run_learner(force=True)
    assert await current_tier_override("very-noisy") == 2  # not straight to 3


async def test_revert_clears_learner_override(client: Any) -> None:
    await _enable("auto")
    await _judged("noisy2", 1, "dismissed", LEARN_MIN_SAMPLE)
    await run_learner(force=True)
    assert await current_tier_override("noisy2") == 2
    resp = await client.post("/api/v1/ambient/policies/revert", json={"category": "noisy2"})
    assert resp.status_code == 200
    assert await current_tier_override("noisy2") is None  # one-click revert


# ── propose mode: queued, applies only on approval ───────────────────


async def test_propose_queues_and_applies_on_approval(client: Any) -> None:
    await _enable("propose")
    await _judged("noisy-prop", 1, "dismissed", LEARN_MIN_SAMPLE)
    changed = await run_learner(force=True)
    assert changed >= 1
    # proposal exists but the override is NOT active
    rows = await _policies("noisy-prop")
    assert rows and rows[-1].source == "learner_proposal"
    assert await current_tier_override("noisy-prop") is None
    # a review item rides the inbox
    async with get_session_factory()() as session:
        review = list(
            (
                await session.execute(select(Delivery).where(Delivery.category == "learning"))
            ).scalars()
        )
    assert review and review[0].tier == 2
    # approval applies it
    approved = await client.post(f"/api/v1/ambient/policies/{rows[-1].id}/approve")
    assert approved.status_code == 200
    assert await current_tier_override("noisy-prop") == 2


async def test_off_mode_collects_only(client: Any) -> None:
    await _enable("off")
    # the M23 static rule needs 5; three dismissals in off mode change nothing
    await _judged("quiet-cat", 1, "dismissed", 3)
    assert await run_learner(force=True) == 0
    assert await _policies("quiet-cat") == []


# ── digest-time shifting, ≤ 2h from the anchored config ──────────────


async def test_digest_shift_toward_acceptance_window_clamped(client: Any) -> None:
    await _enable("auto", ambient_digest_times=["09:00", "17:00"])
    now = datetime.now(UTC)
    # accepted digest items cluster at 20:00 — far past 17:00; shift clamps to +2h
    async with get_session_factory()() as session:
        for i in range(6):
            d = Delivery(
                category="routine",
                tier=2,
                urgency=2,
                title=f"evening {i}",
                channel="digest",
                delivered_at=now.replace(hour=20, minute=0) - timedelta(days=i),
                feedback="accepted",
                reward=1.0,
            )
            session.add(d)
        await session.commit()
    await run_learner(force=True)
    got = (await _settings())["ambient_digest_times"]
    assert got[1] == "19:00"  # 17:00 + 2h clamp, not 20:00
    assert got[0] == "09:00"  # untouched
    rows = await _policies("setting:ambient_digest_times")
    assert rows and rows[-1].source == "learner"
    # the anchor is ledgered — a second pass cannot drift past it
    await run_learner(force=True)
    got2 = (await _settings())["ambient_digest_times"]
    assert got2[1] == "19:00"


async def _settings() -> dict[str, Any]:
    async with get_session_factory()() as session:
        return await get_settings(session)


# ── per-intent judge thresholds via budget.min_urgency ───────────────


async def test_intent_threshold_raised_on_dismissed_fires(client: Any) -> None:
    await _enable("auto")
    async with get_session_factory()() as session:
        intent = StandingIntent(text="w", condition_type="event", status="active")
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
        intent_id = intent.id
    for i in range(LEARN_MIN_SAMPLE):
        d = await add_delivery(
            category="watch", tier=2, urgency=3, title=f"w {i}", intent_id=intent_id
        )
        await record_feedback(d.id, "dismissed")
    await run_learner(force=True)
    async with get_session_factory()() as session:
        fresh = await session.get(StandingIntent, intent_id)
        assert fresh is not None
        assert (fresh.budget or {}).get("min_urgency") == 3  # raised from default 2


async def test_decide_honors_intent_min_urgency(client: Any) -> None:
    from app.ambient.decide import process_event
    from app.ambient.store import emit_event
    from app.llm import fake as fake_llm

    await _enable("auto", default_model="fake:scripted")
    async with get_session_factory()() as session:
        intent = StandingIntent(
            text="only urgent things",
            condition_type="event",
            status="active",
            compiled={
                "match": "events",
                "filters": [{"field": "kind", "op": "equals", "value": "thing"}],
            },
            semantic_predicate="is it urgent?",
            budget={"min_urgency": 4},
        )
        session.add(intent)
        await session.commit()
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "u1",
                "name": "SignificanceOutput",
                "args": {"significant": True, "urgency": 3, "reason": "matches_watch"},
            }
        ],
    )
    event = await emit_event(kind="thing", source="internal", payload={"x": 1})
    assert event is not None
    verdict, reason, _decision = await process_event(event)
    assert verdict == "held"
    assert "min_urgency" in reason


# ── apply_policy is the single application path ─────────────────────


async def test_apply_policy_rejects_tier0(client: Any) -> None:
    await _enable("auto")
    with pytest.raises(ValueError, match="tier 0"):
        await apply_policy(category="x", tier_override=0, reason="bad", source="learner")
