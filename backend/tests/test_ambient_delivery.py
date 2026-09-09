"""Delivery-plane tests (spec §17.5/§17.6/§17.7 substrate — milestone M23):
tiered flushing, budgets + quiet hours, digest builder + return-flush,
supersede-collapse, feedback → blended reward, precision auto-downgrade,
and the anticipation job's gates."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.ambient.deliver import (
    add_delivery,
    apply_precision_rule,
    compute_reward,
    current_tier_override,
    flush_deliveries,
    in_quiet_hours,
    on_user_returned,
    record_feedback,
)
from app.db import get_session_factory
from app.models import AmbientPolicy, Conversation, Delivery, Run
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable(**kv: Any) -> None:
    await _set(ambient_enabled=True, **kv)


async def _get(delivery_id: Any) -> Delivery:
    async with get_session_factory()() as session:
        row = await session.get(Delivery, delivery_id)
        assert row is not None
        return row


DAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _at(hh: int, mm: int = 0) -> datetime:
    return DAY.replace(hour=hh, minute=mm)


# ── quiet hours (spec §17.5: absolute) ───────────────────────────────


def test_quiet_hours_wraparound() -> None:
    ranges = ["22:00", "07:00"]
    assert in_quiet_hours(_at(23), ranges) is True
    assert in_quiet_hours(_at(3), ranges) is True
    assert in_quiet_hours(_at(12), ranges) is False
    assert in_quiet_hours(_at(7, 30), ["08:00", "09:00"]) is False
    assert in_quiet_hours(_at(8, 30), ["08:00", "09:00"]) is True


# ── tier 0: budget debit, over-budget + quiet ⇒ digest-lead ─────────


async def test_interrupt_budget_debit_and_overflow(client: Any) -> None:
    await _enable(
        ambient_notification_budget_per_day=1,
        ambient_quiet_hours=["23:59", "00:00"],
        ambient_digest_times=["23:00"],  # keep the digest out of this pass
    )
    now = _at(12)
    a = await add_delivery(category="failure", tier=0, urgency=5, title="first hard failure")
    b = await add_delivery(category="failure2", tier=0, urgency=5, title="second hard failure")
    await flush_deliveries(now=now)
    a2, b2 = await _get(a.id), await _get(b.id)
    # exactly one interrupt delivered (budget 1); the other leads the digest
    delivered = [d for d in (a2, b2) if d.channel == "interrupt"]
    demoted = [d for d in (a2, b2) if d.channel is None and d.tier == 2]
    assert len(delivered) == 1 and delivered[0].delivered_at is not None
    assert len(demoted) == 1 and demoted[0].urgency == 5  # leads by urgency


async def test_interrupt_quiet_hours_suppressed_to_digest_lead(client: Any) -> None:
    await _enable(ambient_quiet_hours=["22:00", "07:00"])
    d = await add_delivery(category="failure", tier=0, urgency=5, title="hard failure at night")
    await flush_deliveries(now=_at(23))
    row = await _get(d.id)
    assert row.delivered_at is None
    assert row.tier == 2  # demoted: leads the next digest
    assert row.urgency == 5


# ── tier 1: bounded deferral + user-returned flush ──────────────────


async def test_notify_flushes_on_bounded_deferral(client: Any) -> None:
    await _enable(ambient_quiet_hours=["23:59", "00:00"])
    d = await add_delivery(
        category="watch",
        tier=1,
        urgency=3,
        title="notify me",
        deliver_no_later_than=_at(12) - timedelta(minutes=1),
    )
    await flush_deliveries(now=_at(12))  # user not present; deadline passed
    row = await _get(d.id)
    assert row.delivered_at is not None and row.channel == "notify"


async def test_notify_waits_before_deadline_without_presence(client: Any) -> None:
    await _enable(ambient_quiet_hours=["23:59", "00:00"])
    d = await add_delivery(
        category="watch",
        tier=1,
        urgency=3,
        title="notify me",
        deliver_no_later_than=_at(12) + timedelta(minutes=25),
    )
    await flush_deliveries(now=_at(12))
    row = await _get(d.id)
    assert row.delivered_at is None


# ── tier 2: digest at configured times; return-flush; micro-absence ──


async def test_digest_flushes_at_configured_time(client: Any) -> None:
    await _enable(ambient_digest_times=["09:00", "17:00"], ambient_quiet_hours=["23:59", "00:00"])
    d1 = await add_delivery(category="routine", tier=2, urgency=2, title="routine result")
    d2 = await add_delivery(category="watch", tier=2, urgency=4, title="watch result")
    await flush_deliveries(now=_at(8, 55))  # before the digest time
    assert (await _get(d1.id)).delivered_at is None
    await flush_deliveries(now=_at(9, 1))  # just past it
    r1, r2 = await _get(d1.id), await _get(d2.id)
    assert r1.channel == "digest" and r2.channel == "digest"
    assert r1.delivered_at is not None
    # a second tick in the same window does not double-digest
    d3 = await add_delivery(category="routine", tier=2, urgency=2, title="late arrival")
    await flush_deliveries(now=_at(9, 3))
    assert (await _get(d3.id)).delivered_at is None


async def test_return_flush_after_long_absence(client: Any) -> None:
    await _enable(ambient_quiet_hours=["23:59", "00:00"])
    d = await add_delivery(category="routine", tier=2, urgency=2, title="while you were away")
    n = await add_delivery(
        category="watch",
        tier=1,
        urgency=3,
        title="pending notify",
        deliver_no_later_than=datetime.now(UTC) + timedelta(minutes=30),
    )
    await on_user_returned(away_s=2 * 3600)  # > 1h ⇒ collapsed digest stack
    row = await _get(d.id)
    assert row.channel == "digest" and row.delivered_at is not None
    assert (await _get(n.id)).channel == "notify"


async def test_micro_absence_flushes_tier1_only(client: Any) -> None:
    await _enable(ambient_quiet_hours=["23:59", "00:00"])
    d2 = await add_delivery(category="routine", tier=2, urgency=2, title="digest item")
    d1 = await add_delivery(
        category="watch",
        tier=1,
        urgency=3,
        title="notify item",
        deliver_no_later_than=datetime.now(UTC) + timedelta(minutes=30),
    )
    await on_user_returned(away_s=120)  # < 5 min micro-absence
    assert (await _get(d2.id)).delivered_at is None
    assert (await _get(d1.id)).channel == "notify"


# ── supersede-collapse + silent tier ────────────────────────────────


async def test_supersede_collapse_same_key(client: Any) -> None:
    await _enable()
    old = await add_delivery(category="routine", tier=2, urgency=2, title="v1", skey="routine:x")
    new = await add_delivery(category="routine", tier=2, urgency=2, title="v2", skey="routine:x")
    stale = await _get(old.id)
    assert stale.superseded_by == new.id
    # superseded items never flush
    await flush_deliveries(now=_at(9, 1))
    assert (await _get(old.id)).delivered_at is None


async def test_silent_tier_is_ledger_only(client: Any) -> None:
    await _enable()
    d = await add_delivery(category="abstained", tier=3, urgency=1, title="ABSTAIN: quiet")
    row = await _get(d.id)
    assert row.delivered_at is not None and row.channel == "silent"


# ── feedback → blended reward (spec §17.7 substrate) ─────────────────


async def test_feedback_rewards_blend(client: Any) -> None:
    await _enable()
    a = await add_delivery(category="watch", tier=2, urgency=3, title="useful item")
    await record_feedback(a.id, "accepted")
    ra = await _get(a.id)
    assert ra.feedback == "accepted" and ra.reward is not None and ra.reward > 0
    b = await add_delivery(category="watch", tier=2, urgency=3, title="noise item")
    await record_feedback(b.id, "dismissed")
    rb = await _get(b.id)
    assert rb.reward is not None and rb.reward < 0
    c = await add_delivery(category="watch", tier=2, urgency=3, title="meh item")
    await record_feedback(c.id, "ignored")
    rc = await _get(c.id)
    assert rc.reward == 0.0


async def test_reward_repetition_decay(client: Any) -> None:
    await _enable()
    now = datetime.now(UTC)
    first = await add_delivery(category="anticipation", tier=2, urgency=2, title="brief 1")
    async with get_session_factory()() as session:
        row = await session.get(Delivery, first.id)
        assert row is not None
        row.delivered_at = now - timedelta(hours=2)
        await session.commit()
    second = await add_delivery(category="anticipation", tier=2, urgency=2, title="brief 2")
    async with get_session_factory()() as session:
        row = await session.get(Delivery, second.id)
        assert row is not None
        row.delivered_at = now
        await session.commit()
    solo = await add_delivery(category="fresh", tier=2, urgency=2, title="fresh item")
    reward_repeat = await compute_reward(await _get(second.id), "accepted")
    reward_fresh = await compute_reward(await _get(solo.id), "accepted")
    assert reward_repeat < reward_fresh  # recovering-bandit shape


async def test_reward_downstream_usefulness(client: Any) -> None:
    await _enable()
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        run = Run(
            conversation_id=conv.id, chat_message="m", status="completed", orchestrator_mode="graph"
        )
        session.add(run)
        await session.commit()
        run_id, conv_id = run.id, conv.id
    d = await add_delivery(category="watch", tier=2, urgency=3, title="engaged", run_id=run_id)
    async with get_session_factory()() as session:
        row = await session.get(Delivery, d.id)
        assert row is not None
        row.delivered_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
        # the user came back to that conversation AFTER delivery
        session.add(
            Run(
                conversation_id=conv_id,
                chat_message="follow-up",
                status="completed",
                orchestrator_mode="graph",
            )
        )
        await session.commit()
    engaged = await compute_reward(await _get(d.id), "accepted")
    plain = await add_delivery(category="watch2", tier=2, urgency=3, title="plain")
    unengaged = await compute_reward(await _get(plain.id), "accepted")
    assert engaged > unengaged


# ── precision auto-downgrade (spec §17.3/§17.6, rule-based) ─────────


async def test_precision_rule_downgrades_category_one_tier(client: Any) -> None:
    await _enable()
    for i in range(5):
        d = await add_delivery(category="noisy", tier=1, urgency=3, title=f"item {i}")
        await record_feedback(d.id, "dismissed")
    override = await current_tier_override("noisy")
    assert override == 2  # one step down from tier 1, never past 3
    async with get_session_factory()() as session:
        policy = (
            (await session.execute(select(AmbientPolicy).order_by(AmbientPolicy.created_at.desc())))
            .scalars()
            .first()
        )
        assert policy is not None and policy.source == "rule"
        assert "precision" in policy.reason
    # new deliveries in the category are demoted on insert
    nxt = await add_delivery(category="noisy", tier=1, urgency=3, title="post-downgrade")
    assert (await _get(nxt.id)).tier == 2


async def test_precision_rule_needs_minimum_sample(client: Any) -> None:
    await _enable()
    for i in range(3):
        d = await add_delivery(category="sparse", tier=1, urgency=3, title=f"item {i}")
        await record_feedback(d.id, "dismissed")
    assert await current_tier_override("sparse") is None


async def test_apply_precision_rule_idempotent(client: Any) -> None:
    await _enable()
    for i in range(6):
        d = await add_delivery(category="noisy2", tier=2, urgency=2, title=f"item {i}")
        await record_feedback(d.id, "dismissed")
    await apply_precision_rule("noisy2")
    await apply_precision_rule("noisy2")
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AmbientPolicy).where(AmbientPolicy.category == "noisy2")
                )
            ).scalars()
        )
    assert len(rows) == 1  # one ledgered change, not one per feedback


# ── anticipation job gates (spec §17.4 idle work) ────────────────────


async def test_anticipation_skips_below_hit_rate_floor(client: Any) -> None:
    await _enable()
    from app.ambient.anticipate import hit_rate_allows

    for i in range(5):
        d = await add_delivery(category="anticipation", tier=2, urgency=2, title=f"brief {i}")
        await record_feedback(d.id, "dismissed")
    assert await hit_rate_allows() is False


async def test_anticipation_runs_with_fake_model(seeded_client: Any) -> None:
    await _enable()
    await _set(default_model="fake:scripted")
    from app.ambient.anticipate import run_anticipation
    from app.llm import fake as fake_llm

    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.commit()
        session.add(
            Run(
                conversation_id=conv.id,
                chat_message="research pgvector index tuning",
                status="completed",
                orchestrator_mode="graph",
                final_answer="use HNSW for recall-heavy workloads",
            )
        )
        await session.commit()
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "ant1",
                "name": "AnticipationOutput",
                "args": {
                    "items": [
                        {
                            "title": "HNSW parameter deep-dive",
                            "note": "Likely follow-up: ef_construction/m trade-offs.",
                        }
                    ]
                },
            }
        ],
    )
    created = await run_anticipation()
    assert created is not None and len(created) == 1
    async with get_session_factory()() as session:
        row = await session.get(Delivery, created[0])
        assert row is not None
        assert row.category == "anticipation" and row.tier == 2
        assert "ef_construction" in (row.body or "")
    # the same idle window never produces a second briefing
    assert await run_anticipation() is None


async def test_delivery_api_inbox_and_feedback(seeded_client: Any) -> None:
    await _enable()
    d = await add_delivery(category="routine", tier=2, urgency=2, title="api item")
    listed = (await seeded_client.get("/api/v1/deliveries")).json()
    assert any(item["id"] == str(d.id) for item in listed["items"])
    resp = await seeded_client.post(
        f"/api/v1/deliveries/{d.id}/feedback", json={"feedback": "accepted"}
    )
    assert resp.status_code == 200
    assert resp.json()["reward"] is not None
    bad = await seeded_client.post(f"/api/v1/deliveries/{d.id}/feedback", json={"feedback": "meh"})
    assert bad.status_code == 422


# ── M43c: the precision rule is a gated feedback consumer (§17.3/§17.7) ──
#
# Capture is always-on; consumption is switchable. With the gate off, a
# dismissal is still recorded (feedback + reward on the row) but the
# category never re-tiers behind the user's back.


async def test_precision_rule_gate_off_captures_without_retier(client: Any) -> None:
    await _enable(ambient_precision_rule_enabled=False)
    last = None
    for i in range(5):
        d = await add_delivery(category="gated", tier=1, urgency=3, title=f"item {i}")
        last = await record_feedback(d.id, "dismissed")
    # capture survived — the raw material for metrics and future learners
    assert last is not None and last.feedback == "dismissed" and last.reward is not None
    # …but the consumer never fired: no override, no ledgered policy row
    assert await current_tier_override("gated") is None
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AmbientPolicy).where(AmbientPolicy.category == "gated")
                )
            ).scalars()
        )
    assert rows == []
    await _enable(ambient_precision_rule_enabled=True)  # restore the default


async def test_precision_rule_gate_default_is_byte_identical(client: Any) -> None:
    """The key defaults to true — the pre-M43c downgrade behavior unchanged."""
    await _enable()
    for i in range(5):
        d = await add_delivery(category="ungated", tier=1, urgency=3, title=f"item {i}")
        await record_feedback(d.id, "dismissed")
    assert await current_tier_override("ungated") == 2
