"""M30 — ambient UI completeness, backend surface (spec §18.5): the
routine-scoped run listing, the webhook-trigger filter fix in decide, the
watch compile/typed-create endpoints, the correlation-chain ledger view,
and the precision sparkline series."""

from typing import Any
from uuid import uuid4

import pytest

from app.ambient.decide import process_event
from app.ambient.store import emit_event
from app.db import get_session_factory
from app.models import AmbientEvent, Conversation, Delivery, Routine, Run, StandingIntent
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _enable() -> None:
    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_enabled": True})


async def _routine(**kw: Any) -> Routine:
    defaults: dict[str, Any] = {"name": f"r-{uuid4().hex[:8]}", "prompt": "p", "status": "active"}
    defaults.update(kw)
    async with get_session_factory()() as session:
        row = Routine(**defaults)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _webhook_fire(routine: Routine, payload: dict[str, Any]) -> AmbientEvent:
    event = await emit_event(
        kind="routine_fire",
        source="webhook",
        payload={"text": None, "payload": payload},
        routine_id=routine.id,
    )
    assert event is not None
    return event


# ── GET /runs?routine_id= (spec §18.5 drawer run history) ────────────


async def test_runs_filtered_by_routine(client: Any) -> None:
    routine_id = uuid4()
    async with get_session_factory()() as session:
        conv = Conversation(title="m30")
        session.add(conv)
        await session.flush()
        mine = Run(
            conversation_id=conv.id,
            chat_message="ambient fire",
            trigger={"routine_id": str(routine_id), "source": "webhook"},
        )
        other = Run(conversation_id=conv.id, chat_message="chat run")
        session.add_all([mine, other])
        await session.commit()
        mine_id = str(mine.id)
    resp = await client.get(f"/api/v1/runs?routine_id={routine_id}")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert ids == [mine_id]


# ── webhook fires match the ROUTINE's stored filters (§18.5 gap) ─────


async def test_webhook_fire_respects_stored_trigger_filters(client: Any) -> None:
    await _enable()
    routine = await _routine(
        triggers=[
            {"type": "webhook", "filters": [{"field": "repo", "op": "equals", "value": "core"}]}
        ]
    )
    hit = await _webhook_fire(routine, {"repo": "core", "action": "push"})
    miss = await _webhook_fire(routine, {"repo": "docs", "action": "push"})
    v_hit, _, _ = await process_event(hit)
    v_miss, reason, _ = await process_event(miss)
    assert v_hit == "fired"
    assert v_miss == "held" and "filter" in reason


async def test_webhook_fire_without_stored_filters_keeps_firing(client: Any) -> None:
    await _enable()
    routine = await _routine(triggers=[{"type": "interval", "seconds": 3600}])
    event = await _webhook_fire(routine, {"anything": "goes"})
    verdict, _, _ = await process_event(event)
    assert verdict == "fired"  # back-compat: no webhook triggers stored


async def test_webhook_fire_any_matching_trigger_admits(client: Any) -> None:
    await _enable()
    routine = await _routine(
        triggers=[
            {"type": "webhook", "filters": [{"field": "repo", "op": "equals", "value": "core"}]},
            {"type": "webhook", "filters": [{"field": "sev", "op": "equals", "value": "high"}]},
        ]
    )
    event = await _webhook_fire(routine, {"sev": "high"})
    verdict, _, _ = await process_event(event)
    assert verdict == "fired"  # second trigger matched


# ── POST /watches/compile + POST /watches (§18.5 page authoring) ─────


async def test_watches_compile_endpoint(seeded_client: Any) -> None:
    from app.llm import fake as fake_llm

    await _enable()
    async with get_session_factory()() as session:
        await update_settings(session, {"default_model": "fake:scripted"})
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "m30-w",
                "name": "WatchCompile",
                "args": {
                    "mode": "events",
                    "filters": [{"field": "kind", "op": "equals", "value": "hitl_aged"}],
                    "echo": "Watch for runs stuck waiting on your input.",
                },
            }
        ],
    )
    resp = await seeded_client.post(
        "/api/v1/watches/compile", json={"text": "tell me when a run is stuck on me"}
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "proposed" and "stuck" in out["interpretation"]
    async with get_session_factory()() as session:
        row = await session.get(StandingIntent, out["intent_id"])
        assert row is not None and row.status == "proposed"
    # UI confirm = the existing PATCH
    ok = await seeded_client.patch(f"/api/v1/watches/{out['intent_id']}", json={"status": "active"})
    assert ok.status_code == 200 and ok.json()["status"] == "active"


async def test_watches_compile_409_when_dark(client: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_enabled": False})
    resp = await client.post("/api/v1/watches/compile", json={"text": "watch something"})
    assert resp.status_code == 409


async def test_watches_typed_create(client: Any) -> None:
    await _enable()
    resp = await client.post(
        "/api/v1/watches",
        json={
            "text": "high-severity payload events",
            "filters": [{"field": "sev", "op": "equals", "value": "high"}],
            "semantic_predicate": "is this about the payments service?",
        },
    )
    assert resp.status_code == 201
    out = resp.json()
    assert out["status"] == "proposed"
    assert out["compiled"] == {
        "match": "events",
        "filters": [{"field": "sev", "op": "equals", "value": "high", "values": []}],
    }


async def test_watches_typed_create_validates(client: Any) -> None:
    await _enable()
    no_filters = await client.post("/api/v1/watches", json={"text": "everything"})
    assert no_filters.status_code == 422
    bad_op = await client.post(
        "/api/v1/watches",
        json={"text": "x", "filters": [{"field": "a", "op": "sounds_like", "value": "b"}]},
    )
    assert bad_op.status_code == 422


# ── ledger correlation chain + precision series (§18.5) ──────────────


async def test_ledger_chain_by_correlation(client: Any) -> None:
    await _enable()
    root = await emit_event(kind="chain_root", source="manual", payload={"n": 0})
    assert root is not None
    child = await emit_event(
        kind="chain_child", source="internal", payload={"n": 1}, caused_by=root
    )
    assert child is not None
    unrelated = await emit_event(kind="chain_other", source="manual", payload={})
    assert unrelated is not None
    resp = await client.get(f"/api/v1/ambient/ledger?correlation_id={root.correlation_id}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    kinds = [i["kind"] for i in items]
    assert kinds == ["chain_root", "chain_child"]  # chain order, cause → effect
    assert [i["depth"] for i in items] == [0, 1]
    assert all(i["correlation_id"] == str(root.correlation_id) for i in items)


async def test_precision_includes_judged_series(client: Any) -> None:
    await _enable()
    async with get_session_factory()() as session:
        for feedback in ["accepted", "dismissed", "accepted"]:
            session.add(
                Delivery(category="m30cat", tier=2, urgency=2, title="t", feedback=feedback)
            )
        await session.commit()
    resp = await client.get("/api/v1/ambient/precision")
    assert resp.status_code == 200
    row = next(i for i in resp.json()["items"] if i["category"] == "m30cat")
    assert row["series"] == [1, 0, 1]  # chronological, accepted=1 dismissed=0
