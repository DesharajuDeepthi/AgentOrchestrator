"""Closed-loop refinement tests (spec §16.7 — milestone M18)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import get_session_factory
from app.memory import remember
from app.memory.episodic import compact_digests, digest_run, recall_digests
from app.memory.feedback import cited_ids, post_run_citation
from app.memory.inject import build_memory_block
from app.memory.rank import recall
from app.models import Memory, MemoryEmbedding, MemoryEntity, MemoryEntityLink, Run, RunDigest
from app.orchestrator.context import RunContext, set_run_context
from app.orchestrator.recorder import RunRecorder
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable() -> None:
    await _set(memory_enabled=True, embedding_model="fake:scripted")


async def _completed_run(message: str, answer: str, conversation_id: Any = None) -> Run:
    from app.orchestrator.runner import create_run

    run = await create_run(conversation_id, message)
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "completed"
        row.final_answer = answer
        await session.commit()
        await session.refresh(row)
        return row


async def _mem(mid: Any) -> Memory:
    async with get_session_factory()() as session:
        row = await session.get(Memory, mid)
        assert row is not None
        return row


def _ctx(run_id: Any, injected: list[str] | None = None) -> RunContext:
    ctx = RunContext(run_id=run_id, mode="graph", recorder=RunRecorder(run_id))
    if injected:
        ctx.injected_memory_ids = list(injected)
    set_run_context(ctx)
    return ctx


# ── citation feedback (§16.7): used beats retrieved ──────────────────


async def test_injection_does_not_bump_access() -> None:
    await _enable()
    m = await remember(text="the deploy branch is release-2026", kind="fact", source="user_stated")
    block, stats = await build_memory_block(
        "which branch do we deploy from", conversation_id=None, surface="planner"
    )
    assert str(m.id) in stats.memory_ids and block
    fresh = await _mem(m.id)
    assert fresh.access_count == 0  # last_accessed_at is server-set at write (decay anchor)


async def test_explicit_recall_still_bumps_access() -> None:
    await _enable()
    m = await remember(
        text="the staging cluster is named aurora", kind="fact", source="user_stated"
    )
    hits = await recall("staging cluster aurora")
    assert any(h.memory.id == m.id for h in hits)
    fresh = await _mem(m.id)
    assert fresh.access_count == 1 and fresh.last_accessed_at is not None


def test_cited_ids_matches_eight_char_prefixes() -> None:
    a, b = str(uuid4()), str(uuid4())
    answer = f"Your dog is Biscuit (memory {a[:8]})."
    assert cited_ids(answer, [a, b]) == [__import__("uuid").UUID(a)]
    assert cited_ids("no citations here", [a, b]) == []


async def test_post_run_citation_reinforces_only_cited() -> None:
    await _enable()
    cited = await remember(
        text="the user's dog is named Biscuit", kind="fact", source="user_stated"
    )
    uncited = await remember(
        text="the user's editor is Neovim", kind="preference", source="user_stated"
    )
    run = await _completed_run(
        "what's my dog's name?", f"Your dog is Biscuit [fact {str(cited.id)[:8]}]."
    )
    _ctx(run.id, injected=[str(cited.id), str(uncited.id)])
    n = await post_run_citation(run.id)
    assert n == 1
    c, u = await _mem(cited.id), await _mem(uncited.id)
    assert c.access_count == 1 and c.importance == 6  # 5 + 1 reinforcement
    assert u.access_count == 0 and u.importance == 5


async def test_post_run_citation_caps_importance_at_ten() -> None:
    await _enable()
    m = await remember(
        text="identity-level: the user's name is Mahesh",
        kind="fact",
        source="user_stated",
        importance=10,
    )
    run = await _completed_run("who am I?", f"You are Mahesh ({str(m.id)[:8]}).")
    _ctx(run.id, injected=[str(m.id)])
    assert await post_run_citation(run.id) == 1
    assert (await _mem(m.id)).importance == 10


async def test_post_run_citation_noop_when_memory_off_or_nothing_injected() -> None:
    await _set(memory_enabled=False)
    run = await _completed_run("hello", "hi")
    _ctx(run.id, injected=["deadbeef-0000-0000-0000-000000000000"])
    assert await post_run_citation(run.id) == 0
    await _enable()
    _ctx(run.id, injected=[])
    assert await post_run_citation(run.id) == 0


# ── digest compaction (§16.7): bounded episodic growth ───────────────


async def _backdate_digest(digest_id: Any, days: int) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            update(RunDigest)
            .where(RunDigest.id == digest_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=days))
        )
        await session.commit()


async def test_compaction_folds_old_digests_and_cleans_embeddings() -> None:
    await _enable()
    r1 = await _completed_run("first old ask about biscuits", "biscuits answer one")
    r2 = await _completed_run(
        "second old ask about biscuits", "biscuits answer two", conversation_id=r1.conversation_id
    )
    r3 = await _completed_run(
        "fresh ask about clusters", "cluster answer", conversation_id=r1.conversation_id
    )
    d1, d2, d3 = await digest_run(r1.id), await digest_run(r2.id), await digest_run(r3.id)
    assert d1 and d2 and d3
    # r1/r2 belong to one old era; r3 is recent
    await _backdate_digest(d1.id, 30)
    await _backdate_digest(d2.id, 20)

    folded = await compact_digests()
    assert folded == 2
    async with get_session_factory()() as session:
        digests = list((await session.execute(select(RunDigest))).scalars())
        period = [d for d in digests if d.kind == "period"]
        runs = [d for d in digests if d.kind == "run"]
        gone_embeddings = list(
            (
                await session.execute(
                    select(MemoryEmbedding).where(
                        MemoryEmbedding.table_ref == "run_digests",
                        MemoryEmbedding.ref_id.in_([d1.id, d2.id]),
                    )
                )
            ).scalars()
        )
    assert len(period) == 1 and period[0].run_id is None
    assert "answer one" in period[0].text and "answer two" in period[0].text
    assert (period[0].signals or {}).get("runs_folded") == 2
    assert period[0].covers_from is not None and period[0].covers_to is not None
    assert [d.id for d in runs] == [d3.id]
    assert gone_embeddings == []


async def test_compaction_merges_into_existing_period_and_recall_finds_it() -> None:
    await _enable()
    r1 = await _completed_run("ancient fact: the mascot is a walrus", "noted the walrus mascot")
    d1 = await digest_run(r1.id)
    assert d1 is not None
    await _backdate_digest(d1.id, 40)
    assert await compact_digests() == 1
    r2 = await _completed_run(
        "later fact: the walrus is named Wally", "noted wally", conversation_id=r1.conversation_id
    )
    d2 = await digest_run(r2.id)
    assert d2 is not None
    await _backdate_digest(d2.id, 20)
    assert await compact_digests() == 1  # merges into the SAME period row
    async with get_session_factory()() as session:
        periods = list(
            (await session.execute(select(RunDigest).where(RunDigest.kind == "period"))).scalars()
        )
    assert len(periods) == 1
    assert (periods[0].signals or {}).get("runs_folded") == 2
    episodes = await recall_digests("walrus mascot", k=3)
    assert any(d.kind == "period" for d, _ in episodes)


async def test_compaction_leaves_recent_digests_alone() -> None:
    await _enable()
    r = await _completed_run("a recent ask", "a recent answer")
    d = await digest_run(r.id)
    assert d is not None
    assert await compact_digests() == 0
    async with get_session_factory()() as session:
        kinds = [x.kind for x in (await session.execute(select(RunDigest))).scalars()]
    assert kinds == ["run"]


# ── entity-hop recall (§16.7): light graph expansion ─────────────────


async def test_remember_with_entities_creates_and_dedupes_links() -> None:
    await _enable()
    a = await remember(
        text="Biscuit is the user's dog",
        kind="entity",
        source="user_stated",
        entities=["Biscuit"],
    )
    b = await remember(
        text="Biscuit was adopted from the Austin shelter",
        kind="fact",
        source="user_stated",
        entities=["biscuit", "Austin shelter"],  # case-insensitive reuse
    )
    async with get_session_factory()() as session:
        entities = list((await session.execute(select(MemoryEntity))).scalars())
        links = list((await session.execute(select(MemoryEntityLink))).scalars())
    assert sorted(e.name.lower() for e in entities) == ["austin shelter", "biscuit"]
    assert {(link.memory_id) for link in links} == {a.id, b.id}
    assert len(links) == 3


async def test_recall_appends_entity_hop_below_direct_hits() -> None:
    await _enable()
    direct = await remember(
        text="Biscuit is the user's dog",
        kind="entity",
        source="user_stated",
        entities=["Biscuit"],
    )
    linked = await remember(
        text="Biscuit came from the Austin shelter in 2020",
        kind="fact",
        source="user_stated",
        entities=["Biscuit"],
    )
    # age the linked memory so it cannot clear the floor on its own —
    # the hop must be what surfaces it (structure, not similarity)
    async with get_session_factory()() as session:
        await session.execute(
            update(Memory)
            .where(Memory.id == linked.id)
            .values(
                recorded_at=datetime.now(UTC) - timedelta(days=300),
                last_accessed_at=datetime.now(UTC) - timedelta(days=300),
            )
        )
        await session.commit()
    hits = await recall("the user's dog", floor=0.5)
    ids = [h.memory.id for h in hits]
    assert direct.id in ids and linked.id in ids
    hop = next(h for h in hits if h.memory.id == linked.id)
    top = next(h for h in hits if h.memory.id == direct.id)
    assert hop.linked is True and top.linked is False
    assert hop.score <= top.score


async def test_entity_hop_skipped_for_kind_filtered_and_as_of_recalls() -> None:
    await _enable()
    await remember(
        text="Biscuit is the user's dog", kind="entity", source="user_stated", entities=["Biscuit"]
    )
    await remember(
        text="Biscuit came from the Austin shelter",
        kind="fact",
        source="user_stated",
        entities=["Biscuit"],
    )
    kinds_hits = await recall("the user's dog", kinds=["entity"])
    assert all(not h.linked for h in kinds_hits)
    as_of_hits = await recall("the user's dog", as_of=datetime.now(UTC))
    assert all(not h.linked for h in as_of_hits)


# ── as_of on the native tool (§16.4, verified for M18) ───────────────


async def test_memory_recall_tool_supports_as_of() -> None:
    from app.memory import supersede
    from app.native.memory_tools import memory_recall

    await _enable()
    old = await remember(text="the deploy branch is main", kind="fact", source="user_stated")
    before_correction = datetime.now(UTC)
    new = await supersede(old.id, text="the deploy branch is release-2026", source="user_stated")
    assert new is not None
    then = await memory_recall("deploy branch", as_of=before_correction.isoformat())
    texts = [m["text"] for m in then["memories"]]
    assert any("main" in t for t in texts)
    assert not any("release-2026" in t for t in texts)


# ── citation-id scrub in the admission gate (§16.7 follow-up) ────────


async def test_gate_scrubs_citation_ids_from_candidate_text() -> None:
    from app.memory.store import Candidate, gate_candidates, scrub_citation_ids

    assert (
        scrub_citation_ids("The deploy branch is `release-2026` (episode 1fcf6bb1).")
        == "The deploy branch is `release-2026`."
    )
    assert scrub_citation_ids("Target missed [fact ab12cd34, ef56ab78] by 25%.").count("[") == 0
    assert scrub_citation_ids("plain fact with no markers") == "plain fact with no markers"
    # a legitimate parenthetical survives
    assert scrub_citation_ids("Vibration fell to 2.1 mm/s (down from 5.8).").endswith(
        "(down from 5.8)."
    )

    await _enable()
    accepted, dropped = await gate_candidates(
        [Candidate(text="The deploy branch is release-2026 (memory deadbeef).", kind="fact")]
    )
    assert dropped == []
    assert accepted[0].text == "The deploy branch is release-2026."
