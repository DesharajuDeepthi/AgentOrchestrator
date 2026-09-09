"""M27 — memory context pack (spec §18.2): routine cross-fire continuity,
the `project` memory scope end-to-end, and the aggregator memory surface."""

from typing import Any
from uuid import uuid4

import pytest

from app.ambient.execute import prepare_run
from app.ambient.store import emit_event
from app.db import get_session_factory
from app.memory.inject import build_memory_block
from app.memory.rank import recall
from app.memory.store import MemoryWriteError, remember
from app.models import AmbientEvent, Conversation, Routine
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _memory_on() -> None:
    await _set(memory_enabled=True, embedding_model=None)


# ── project scope (spec §18.2) ───────────────────────────────────────


async def test_project_scope_requires_key(client: Any) -> None:
    await _memory_on()
    with pytest.raises(MemoryWriteError, match="project"):
        await remember(
            text="orphan project fact", kind="fact", scope="project", source="user_stated"
        )
    row = await remember(
        text="apollo launch window opens Thursday 06:00 UTC",
        kind="fact",
        scope="project",
        source="user_stated",
        project_key="apollo",
    )
    assert row.scope == "project" and row.project_key == "apollo"


async def test_recall_filters_project_scope(client: Any) -> None:
    await _memory_on()
    await remember(
        text="apollo booster uses RP-1 fuel",
        kind="fact",
        scope="project",
        source="user_stated",
        project_key="apollo",
    )
    await remember(
        text="zeus booster uses methane fuel",
        kind="fact",
        scope="project",
        source="user_stated",
        project_key="zeus",
    )
    await remember(
        text="booster telemetry is on channel 7", kind="fact", scope="global", source="user_stated"
    )
    apollo = await recall("booster fuel telemetry", project_key="apollo", floor=0.0)
    texts = [h.memory.text for h in apollo]
    assert any("RP-1" in t for t in texts)
    assert not any("methane" in t for t in texts)  # other projects never leak
    assert any("channel 7" in t for t in texts)  # global still visible
    unscoped = await recall("booster fuel", floor=0.0)
    assert not any("RP-1" in t or "methane" in t for t in [h.memory.text for h in unscoped])


async def test_conversation_project_key_reaches_injection(client: Any) -> None:
    await _memory_on()
    await remember(
        text="apollo launch window opens Thursday 06:00 UTC",
        kind="fact",
        scope="project",
        source="user_stated",
        project_key="apollo",
    )
    async with get_session_factory()() as session:
        conv_in = Conversation(title="apollo planning", project_key="apollo")
        conv_out = Conversation(title="unrelated")
        session.add_all([conv_in, conv_out])
        await session.commit()
        in_id, out_id = conv_in.id, conv_out.id
    block_in, stats_in = await build_memory_block(
        "when does the launch window open", conversation_id=in_id, surface="planner"
    )
    assert "Thursday 06:00" in block_in and stats_in.memories >= 1
    block_out, _ = await build_memory_block(
        "when does the launch window open", conversation_id=out_id, surface="planner"
    )
    assert "Thursday 06:00" not in block_out


async def test_chat_creates_conversation_with_project(seeded_client: Any) -> None:
    from app.llm import fake as fake_llm

    await _set(default_model="fake:scripted", formatter_enabled=False, orchestrator_mode="graph")
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "PlannerOutput",
                "args": {"entries": [], "direct_answer": "ok", "no_confident_match": False},
                "id": "m27-1",
            }
        ],
    )
    resp = await seeded_client.post("/api/v1/chat", json={"message": "hello", "project": "apollo"})
    assert resp.status_code == 201
    conv_id = (await seeded_client.get(f"/api/v1/runs/{resp.json()['run_id']}")).json()[
        "conversation_id"
    ]
    async with get_session_factory()() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None and conv.project_key == "apollo"


async def test_memory_tool_remember_project(client: Any) -> None:
    from app.native.memory_tools import memory_remember

    await _memory_on()
    out = await memory_remember(
        text="apollo retro thrusters need a 3s warmup",
        kind="fact",
        scope="project",
        project="apollo",
    )
    assert out["stored"] is True
    assert out["memory"]["scope"] == "project"


# ── routine cross-fire continuity (spec §18.2) ───────────────────────


async def _fired_event(routine: Routine) -> AmbientEvent:
    event = await emit_event(kind="routine_fire", source="webhook", routine_id=routine.id)
    assert event is not None
    async with get_session_factory()() as session:
        row = await session.get(AmbientEvent, event.id)
        assert row is not None
        row.verdict = "fired"
        row.decision = {"fired_for": "routine"}
        await session.commit()
        await session.refresh(row)
        return row


async def test_routine_include_memories_reuses_one_conversation(client: Any) -> None:
    await _set(ambient_enabled=True)
    async with get_session_factory()() as session:
        routine = Routine(name=f"r-{uuid4().hex[:8]}", prompt="p", include_memories=True)
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
    run1 = await prepare_run(await _fired_event(routine))
    run2 = await prepare_run(await _fired_event(routine))
    assert run1 is not None and run2 is not None
    assert run1.conversation_id == run2.conversation_id  # ONE persistent conversation
    assert run1.include_memories and run2.include_memories
    async with get_session_factory()() as session:
        fresh = await session.get(Routine, routine.id)
        assert fresh is not None and fresh.conversation_id == run1.conversation_id


async def test_routine_default_stays_fresh_per_fire(client: Any) -> None:
    await _set(ambient_enabled=True)
    async with get_session_factory()() as session:
        routine = Routine(name=f"r-{uuid4().hex[:8]}", prompt="p")
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
    run1 = await prepare_run(await _fired_event(routine))
    run2 = await prepare_run(await _fired_event(routine))
    assert run1 is not None and run2 is not None
    assert run1.conversation_id != run2.conversation_id


async def test_persistent_conversation_carries_history(client: Any) -> None:
    """The continuity mechanism itself: the second fire's history contains
    the first fire's answer."""
    from app.orchestrator.graph_mode import build_history

    await _set(ambient_enabled=True)
    async with get_session_factory()() as session:
        routine = Routine(name=f"r-{uuid4().hex[:8]}", prompt="p", include_memories=True)
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
    run1 = await prepare_run(await _fired_event(routine))
    assert run1 is not None
    async with get_session_factory()() as session:
        from app.models import Run

        row = await session.get(Run, run1.id)
        assert row is not None
        row.status = "completed"
        row.final_answer = "beat-one: conveyor nominal at 2.1 m/s"
        await session.commit()
    run2 = await prepare_run(await _fired_event(routine))
    assert run2 is not None
    history = await build_history(run2.conversation_id)
    assert "beat-one" in history


# ── aggregator memory surface (spec §18.2) ───────────────────────────


async def test_aggregator_memory_block_on_and_dark(client: Any) -> None:
    from app.orchestrator.graph_mode import aggregator_memory_block

    await _memory_on()
    await remember(
        text="when you summarize findings, use bullet points",
        kind="preference",
        scope="global",
        source="user_stated",
    )
    block = await aggregator_memory_block("summarize the findings")
    assert "bullet points" in block
    await _set(memory_enabled=False)
    assert await aggregator_memory_block("summarize the findings") == ""
