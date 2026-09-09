"""M31 — memory communities (spec §18.6): label propagation over entity
links with deterministic tie-breaks, generative summaries kept fresh
incrementally, and community breadth in recall injection."""

from typing import Any
from uuid import UUID

import pytest

from app.db import get_session_factory
from app.memory.communities import communities_for_memories, rebuild_communities
from app.memory.inject import build_memory_block
from app.memory.store import remember
from app.models import MemoryCommunity, MemoryEntity, MemoryEntityLink
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _memory_on() -> None:
    await _set(memory_enabled=True, embedding_model=None, default_model="fake:scripted")


async def _entity(name: str) -> MemoryEntity:
    async with get_session_factory()() as session:
        row = MemoryEntity(name=name)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _link(memory_id: UUID, entity_id: UUID) -> None:
    async with get_session_factory()() as session:
        session.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id))
        await session.commit()


async def _cluster(names: list[str], texts: list[str]) -> tuple[list[MemoryEntity], list[UUID]]:
    """Entities co-mentioned across memories form one connected component."""
    entities = [await _entity(n) for n in names]
    memory_ids = []
    for text in texts:
        m = await remember(text=text, kind="fact", scope="global", source="user_stated")
        memory_ids.append(m.id)
        for e in entities:
            await _link(m.id, e.id)
    return entities, memory_ids


async def _rows() -> list[MemoryCommunity]:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        return list((await session.execute(select(MemoryCommunity))).scalars())


# ── label propagation (spec §18.6) ───────────────────────────────────


async def test_two_disjoint_clusters_become_two_communities(client: Any) -> None:
    from app.llm import fake as fake_llm

    await _memory_on()
    apollo, _ = await _cluster(
        ["apollo", "booster", "launchpad"],
        ["apollo booster fueling starts at dawn", "the launchpad crew preps apollo"],
    )
    zeus, _ = await _cluster(["zeus", "gateway"], ["zeus routes through the new gateway"])
    fake_llm.push_ai("Apollo launch operations: booster and pad readiness.")
    fake_llm.push_ai("Zeus networking: gateway routing.")
    built = await rebuild_communities()
    assert built == 2
    rows = await _rows()
    assert len(rows) == 2
    sizes = sorted(len(r.member_entity_ids or []) for r in rows)
    assert sizes == [2, 3]
    # deterministic representative label: the min entity id of the community
    for row in rows:
        assert row.label == min(str(e) for e in (row.member_entity_ids or []))
    assert all(r.summary for r in rows)


async def test_rebuild_is_incremental_unchanged_graph_keeps_summaries(client: Any) -> None:
    from app.llm import fake as fake_llm

    await _memory_on()
    await _cluster(["hermes", "relay"], ["hermes speaks through the relay"])
    fake_llm.push_ai("Hermes relay comms.")
    assert await rebuild_communities() == 1
    first = (await _rows())[0]
    # second pass: NO fake response queued — a summary regeneration would fail
    assert await rebuild_communities() == 1
    second = (await _rows())[0]
    assert second.summary == first.summary and second.id == first.id


async def test_link_change_updates_membership_and_summary(client: Any) -> None:
    from app.llm import fake as fake_llm

    await _memory_on()
    entities, memory_ids = await _cluster(["atlas", "crane"], ["atlas lifts with the crane"])
    fake_llm.push_ai("Atlas lifting ops.")
    await rebuild_communities()
    # a new entity joins the component via a shared memory
    winch = await _entity("winch")
    await _link(memory_ids[0], winch.id)
    fake_llm.push_ai("Atlas lifting ops now including the winch.")
    assert await rebuild_communities() == 1
    row = (await _rows())[0]
    assert len(row.member_entity_ids or []) == 3
    assert row.summary is not None and "winch" in row.summary


async def test_singletons_and_empty_graph_are_noops(client: Any) -> None:
    await _memory_on()
    assert await rebuild_communities() == 0  # empty graph ⇒ no-op
    lone = await _entity("hermit")
    m = await remember(
        text="the hermit stands alone", kind="fact", scope="global", source="user_stated"
    )
    await _link(m.id, lone.id)
    assert await rebuild_communities() == 0  # singleton entities form no community
    assert await _rows() == []


# ── community breadth in recall (spec §18.6) ─────────────────────────


async def test_injection_includes_community_summary(client: Any) -> None:
    from app.llm import fake as fake_llm

    await _memory_on()
    _, memory_ids = await _cluster(
        ["orion", "capsule", "heatshield"],
        [
            "orion capsule passed the vacuum soak",
            "the orion heatshield ablation margin is 22 percent",
        ],
    )
    fake_llm.push_ai("Orion program status: capsule qualified, heatshield margins healthy.")
    await rebuild_communities()
    found = await communities_for_memories(memory_ids)
    assert len(found) == 1 and found[0].summary is not None

    block, stats = await build_memory_block(
        "how is the orion capsule heatshield doing", conversation_id=None, surface="planner"
    )
    assert "Orion program status" in block  # the community summary section
    assert stats.communities == 1


async def test_community_budget_zero_drops_section(client: Any) -> None:
    from app.llm import fake as fake_llm

    await _memory_on()
    await _cluster(["vulcan", "engine"], ["vulcan engine test fired for 30s"])
    fake_llm.push_ai("Vulcan propulsion notes.")
    await rebuild_communities()
    await _set(memory_community_budget_tokens=0)
    block, stats = await build_memory_block(
        "vulcan engine test status", conversation_id=None, surface="planner"
    )
    assert "Vulcan propulsion" not in block
    assert stats.communities == 0
