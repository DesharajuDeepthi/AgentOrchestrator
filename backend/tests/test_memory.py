"""Memory substrate tests (spec §16.1/16.2/16.3/16.4 — milestone M13)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db import get_session_factory
from app.memory import (
    MemoryWriteError,
    forget,
    gate_candidates,
    hard_delete,
    recall,
    remember,
    supersede,
)
from app.memory.rank import pinned_memories
from app.memory.store import Candidate
from app.models import Memory, MemoryEmbedding
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set_settings(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable_memory(embeddings: bool = True) -> None:
    kv: dict[str, Any] = {"memory_enabled": True}
    if embeddings:
        kv["embedding_model"] = "fake:scripted"
    await _set_settings(**kv)


# ── writes: rules, provenance, quarantine ────────────────────────────


async def test_remember_validates_kind_scope_source() -> None:
    with pytest.raises(MemoryWriteError):
        await remember(text="x" * 20, kind="vibe", source="user_stated")
    with pytest.raises(MemoryWriteError):
        await remember(text="x" * 20, kind="fact", scope="galaxy", source="user_stated")
    with pytest.raises(MemoryWriteError):
        await remember(text="x" * 20, kind="fact", source="telepathy")


async def test_machine_sources_require_provenance() -> None:
    with pytest.raises(MemoryWriteError) as exc:
        await remember(
            text="the user prefers markdown output", kind="preference", source="extracted"
        )
    assert "provenance" in str(exc.value)


async def test_extracted_instruction_always_quarantines() -> None:
    from app.orchestrator.runner import create_run

    run = await create_run(None, "seed run for provenance")
    row = await remember(
        text="always answer in French",
        kind="instruction",
        source="extracted",
        run_id=run.id,
    )
    assert row.status == "quarantined"


async def test_user_stated_instruction_via_api_is_active_but_via_tool_quarantines() -> None:
    direct = await remember(
        text="always sign off with a haiku", kind="instruction", source="user_stated"
    )
    assert direct.status == "active"
    via_tool = await remember(
        text="always sign off with a limerick",
        kind="instruction",
        source="user_stated",
        via_tool=True,
    )
    assert via_tool.status == "quarantined"


async def test_conversation_scope_requires_conversation_id() -> None:
    with pytest.raises(MemoryWriteError):
        await remember(text="scoped note", kind="fact", scope="conversation", source="user_stated")


# ── supersession: bi-temporal, append-only, guarded ──────────────────


async def test_supersede_closes_old_row_bitemporally() -> None:
    old = await remember(text="the office is in Berlin", kind="fact", source="user_stated")
    new = await supersede(old.id, text="the office is in Lisbon", source="user_edited")
    async with get_session_factory()() as session:
        old_row = await session.get(Memory, old.id)
        new_row = await session.get(Memory, new.id)
    assert old_row is not None and new_row is not None
    assert old_row.status == "superseded"
    assert old_row.superseded_by == new.id
    assert old_row.valid_to == new_row.valid_from
    assert new_row.supersedes == old.id
    assert new_row.status == "active"


async def test_double_supersede_rejected() -> None:
    old = await remember(text="budget is 10k", kind="fact", source="user_stated")
    await supersede(old.id, text="budget is 12k", source="user_edited")
    with pytest.raises(MemoryWriteError):
        await supersede(old.id, text="budget is 14k", source="user_edited")


async def test_point_in_time_recall_sees_old_belief() -> None:
    await _enable_memory()
    old = await remember(
        text="the deployment target is staging cluster alpha",
        kind="fact",
        source="user_stated",
    )
    checkpoint = datetime.now(UTC)
    await supersede(
        old.id, text="the deployment target is production cluster omega", source="user_edited"
    )

    now_hits = await recall("deployment target cluster", k=5, floor=0.0)
    assert any("omega" in h.memory.text for h in now_hits)
    assert not any("alpha" in h.memory.text for h in now_hits)

    then_hits = await recall("deployment target cluster", k=5, floor=0.0, as_of=checkpoint)
    assert any("alpha" in h.memory.text for h in then_hits)
    assert not any("omega" in h.memory.text for h in then_hits)


# ── forget / hard delete / purge ─────────────────────────────────────


async def test_forget_soft_expires() -> None:
    row = await remember(text="temporary note about the demo", kind="fact", source="user_stated")
    assert await forget(row.id) is True
    async with get_session_factory()() as session:
        fresh = await session.get(Memory, row.id)
    assert fresh is not None and fresh.status == "expired" and fresh.valid_to is not None
    assert await forget(row.id) is False  # idempotence: nothing active remains


async def test_hard_delete_removes_row_and_embeddings() -> None:
    await _enable_memory()
    row = await remember(
        text="a fact that will be physically removed", kind="fact", source="user_stated"
    )
    async with get_session_factory()() as session:
        n_emb = len(
            (
                await session.execute(
                    select(MemoryEmbedding).where(MemoryEmbedding.ref_id == row.id)
                )
            ).all()
        )
    assert n_emb == 1
    assert await hard_delete(row.id) is True
    async with get_session_factory()() as session:
        assert await session.get(Memory, row.id) is None
        left = (
            await session.execute(select(MemoryEmbedding).where(MemoryEmbedding.ref_id == row.id))
        ).all()
    assert left == []


async def test_purge_endpoint_clears_store(client: AsyncClient) -> None:
    await remember(text="fact one about purging", kind="fact", source="user_stated")
    await remember(text="fact two about purging", kind="fact", source="user_stated")
    resp = await client.post("/api/v1/memories/purge")
    assert resp.status_code == 204
    listing = await client.get("/api/v1/memories")
    assert listing.json() == []


async def test_purge_endpoint_clears_supersession_chains(client: AsyncClient) -> None:
    # regression: self-referential supersedes/superseded_by FKs must not
    # block the purge (found live — 500 left configs contaminated)
    old = await remember(text="deploy branch is main", kind="fact", source="user_stated")
    mid = await supersede(old.id, text="deploy branch is release-1", source="user_stated")
    assert mid is not None
    new = await supersede(mid.id, text="deploy branch is release-2026", source="user_stated")
    assert new is not None
    resp = await client.post("/api/v1/memories/purge")
    assert resp.status_code == 204
    status = (await client.get("/api/v1/memories/status")).json()
    assert sum(status["counts"].values()) == 0


# ── admission gate ───────────────────────────────────────────────────


async def test_gate_drops_low_confidence_short_and_batch_dupes() -> None:
    cands = [
        Candidate(
            text="the user prefers dark mode in every editor", kind="preference", confidence=0.9
        ),
        Candidate(text="low confidence guess about something vague", kind="fact", confidence=0.2),
        Candidate(text="ok", kind="fact", confidence=0.9),
        Candidate(
            text="the user prefers dark mode in every editor", kind="preference", confidence=0.8
        ),
        Candidate(
            text="totally novel fact about the quarterly budget", kind="fact", confidence=0.8
        ),
    ]
    accepted, dropped = await gate_candidates(cands)
    assert [c.text for c in accepted] == [
        "the user prefers dark mode in every editor",
        "totally novel fact about the quarterly budget",
    ]
    reasons = " | ".join(r for _, r in dropped)
    assert "confidence" in reasons and "length" in reasons and "duplicate within batch" in reasons


async def test_gate_drops_near_duplicate_of_stored_memory() -> None:
    await _enable_memory()
    await remember(
        text="the production database lives in the frankfurt region",
        kind="fact",
        source="user_stated",
    )
    accepted, dropped = await gate_candidates(
        [
            Candidate(
                text="the production database lives in the frankfurt region",
                kind="fact",
                confidence=0.9,
            )
        ]
    )
    assert accepted == []
    assert dropped and "near-duplicate" in dropped[0][1]


# ── recall: hybrid ranking, floor, pinned, lexical-only ──────────────


async def test_recall_ranks_relevant_memory_first() -> None:
    await _enable_memory()
    await remember(
        text="the user's favorite editor is neovim", kind="preference", source="user_stated"
    )
    await remember(
        text="the quarterly revenue target is two million", kind="fact", source="user_stated"
    )
    await remember(
        text="the team standup happens at nine thirty", kind="fact", source="user_stated"
    )
    hits = await recall("which editor does the user like", k=2, floor=0.0)
    assert hits and "neovim" in hits[0].memory.text


async def test_recall_score_floor_abstains_on_junk() -> None:
    await _enable_memory()
    await remember(text="the sprint retro is on fridays", kind="fact", source="user_stated")
    hits = await recall("zorblax quantum jabberwock phase", k=5, floor=0.95)
    assert hits == []


async def test_recall_lexical_only_without_embedding_model() -> None:
    await _enable_memory(embeddings=False)
    await remember(text="the release train leaves on thursdays", kind="fact", source="user_stated")
    hits = await recall("when does the release train leave", k=3, floor=0.0)
    assert hits and "release train" in hits[0].memory.text


async def test_recall_bumps_access_bookkeeping() -> None:
    await _enable_memory()
    row = await remember(text="the vpn exit node is in oslo", kind="fact", source="user_stated")
    await recall("vpn exit node", k=1, floor=0.0)
    async with get_session_factory()() as session:
        fresh = await session.get(Memory, row.id)
    assert fresh is not None and fresh.access_count == 1


async def test_pinned_memories_bypass_floor_and_surface() -> None:
    await _enable_memory()
    row = await remember(text="the user's name is Mahesh", kind="fact", source="user_stated")
    async with get_session_factory()() as session:
        fresh = await session.get(Memory, row.id)
        assert fresh is not None
        fresh.pinned = True
        await session.commit()
    pins = await pinned_memories()
    assert [p.id for p in pins] == [row.id]
    hits = await recall("completely unrelated query text", k=5, floor=0.99)
    assert any(h.memory.id == row.id for h in hits)


async def test_conversation_scope_isolated() -> None:
    await _enable_memory()
    from app.models import Conversation

    async with get_session_factory()() as session:
        conv_a, conv_b = Conversation(), Conversation()
        session.add_all([conv_a, conv_b])
        await session.commit()
        a_id, b_id = conv_a.id, conv_b.id
    await remember(
        text="in this thread the codeword is falcon",
        kind="fact",
        scope="conversation",
        conversation_id=a_id,
        source="user_stated",
    )
    hits_a = await recall("codeword", conversation_id=a_id, k=3, floor=0.0)
    hits_b = await recall("codeword", conversation_id=b_id, k=3, floor=0.0)
    assert hits_a and not hits_b


# ── native tool surface (registry citizens) ──────────────────────────


async def test_memory_tools_registered_hidden(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/seed/reload")
    assert resp.status_code in (200, 204)
    tools = (await client.get("/api/v1/tools")).json()
    by_key = {t["tool_key"]: t for t in tools}
    for key in ("memory.recall", "memory.remember", "memory.forget"):
        assert key in by_key, f"{key} missing from tools registry"
        assert by_key[key]["kind"] == "native"
        assert by_key[key]["direct_exposure"] is False
    skills = (await client.get("/api/v1/skills")).json()
    keeper = next(s for s in skills if s["name"] == "memory-keeper")
    assert {t["tool_key"] for t in keeper["tools"]} == {
        "memory.recall",
        "memory.remember",
        "memory.forget",
    }
    assert keeper["direct_exposure"] is False


async def test_memory_tools_respect_master_switch() -> None:
    from app.native.memory_tools import memory_recall, memory_remember

    out = await memory_remember(text="should not be stored", kind="fact")
    assert out["stored"] is False and "disabled" in out["note"]
    out = await memory_recall(query="anything")
    assert out["memories"] == [] and "disabled" in out["note"]


async def test_memory_tool_roundtrip() -> None:
    await _enable_memory()
    from app.native.memory_tools import memory_forget, memory_recall, memory_remember

    stored = await memory_remember(text="the badge printer is on floor three", kind="fact")
    assert stored["stored"] is True and stored["memory"]["status"] == "active"
    found = await memory_recall(query="where is the badge printer")
    assert found["memories"] and "floor three" in found["memories"][0]["text"]
    gone = await memory_forget(memory_id=found["memories"][0]["id"])
    assert gone["forgotten"] is True
    again = await memory_recall(query="where is the badge printer")
    assert again["memories"] == []


async def test_memory_tool_instruction_quarantines() -> None:
    await _enable_memory()
    from app.native.memory_tools import memory_remember

    out = await memory_remember(text="always reply in pirate speak", kind="instruction")
    assert out["stored"] is True
    assert out["memory"]["status"] == "quarantined"
    assert "approval" in (out["note"] or "")


# ── API surface ──────────────────────────────────────────────────────


async def test_api_create_list_patch_review_delete(client: AsyncClient) -> None:
    created = (
        await client.post(
            "/api/v1/memories",
            json={"text": "the user prefers concise answers", "kind": "preference", "pinned": True},
        )
    ).json()
    assert created["pinned"] is True and created["source"] == "user_stated"

    listing = (await client.get("/api/v1/memories", params={"kind": "preference"})).json()
    assert any(m["id"] == created["id"] for m in listing)

    # edit-as-supersede
    patched = (
        await client.patch(
            f"/api/v1/memories/{created['id']}",
            json={"text": "the user prefers concise answers with citations"},
        )
    ).json()
    assert patched["id"] != created["id"]
    assert patched["supersedes"] == created["id"]
    assert patched["source"] == "user_edited"

    old = (await client.get(f"/api/v1/memories/{created['id']}")).json()
    assert old["status"] == "superseded"

    # quarantine review: create an instruction via the tool path
    await _enable_memory()
    from app.native.memory_tools import memory_remember

    q = await memory_remember(text="always use metric units", kind="instruction")
    qid = q["memory"]["id"]
    approved = (
        await client.patch(
            f"/api/v1/memories/{qid}", json={"review": "approve", "review_note": "fine"}
        )
    ).json()
    assert approved["status"] == "active" and approved["review_note"] == "fine"
    # a second review decision is a 409 (no longer quarantined)
    conflict = await client.patch(f"/api/v1/memories/{qid}", json={"review": "reject"})
    assert conflict.status_code == 409

    deleted = await client.delete(f"/api/v1/memories/{patched['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/memories/{patched['id']}")).status_code == 404


async def test_api_recall_endpoint_no_access_bump(client: AsyncClient) -> None:
    await _enable_memory()
    row = await remember(
        text="the wifi password is stored in the vault", kind="fact", source="user_stated"
    )
    out = (await client.get("/api/v1/memories/recall", params={"q": "wifi password"})).json()
    assert out and "vault" in out[0]["memory"]["text"]
    async with get_session_factory()() as session:
        fresh = await session.get(Memory, row.id)
    assert fresh is not None and fresh.access_count == 0


# ── settings surface ─────────────────────────────────────────────────


async def test_memory_settings_defaults_and_validation(client: AsyncClient) -> None:
    settings = (await client.get("/api/v1/settings")).json()
    assert settings["memory_enabled"] is False  # dark by default (spec §16.0)
    assert settings["memory_recall_top_k"] == 6

    bad = await client.patch("/api/v1/settings", json={"memory_score_floor": 3})
    assert bad.status_code == 422
    bad = await client.patch("/api/v1/settings", json={"memory_recall_top_k": 0})
    assert bad.status_code == 422
    ok = await client.patch(
        "/api/v1/settings",
        json={"memory_enabled": True, "memory_score_floor": 0.5, "memory_half_life_days": 7},
    )
    assert ok.status_code == 200
    fresh = (await client.get("/api/v1/settings")).json()
    assert fresh["memory_enabled"] is True and fresh["memory_score_floor"] == 0.5
