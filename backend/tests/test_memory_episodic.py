"""Episodic layer + injection plane tests (spec §16.2/16.3 — milestone M14)."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db import get_session_factory
from app.memory import remember
from app.memory.episodic import digest_run, recall_digests, update_rollup
from app.memory.inject import build_memory_block
from app.memory.scheduler import acquire_job_lock, process_run, release_job_lock
from app.models import Conversation, ConversationRollup, Run, RunDigest
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable(embeddings: bool = True) -> None:
    kv: dict[str, Any] = {"memory_enabled": True}
    if embeddings:
        kv["embedding_model"] = "fake:scripted"
    await _set(**kv)


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


# ── digests ──────────────────────────────────────────────────────────


async def test_digest_created_with_mechanical_fallback_and_signals() -> None:
    await _enable()
    run = await _completed_run(
        "summarize the quarterly revenue numbers", "revenue was 1.2M in june"
    )
    digest = await digest_run(run.id)
    assert digest is not None
    assert "quarterly revenue" in digest.text
    assert digest.signals is not None
    assert digest.signals["status"] == "completed"
    assert digest.signals["mode"] in {"graph", "agentic", "direct"}
    assert digest.signals["denied"] is False


async def test_digest_idempotent_per_run() -> None:
    await _enable()
    run = await _completed_run("idempotence check message", "done")
    first = await digest_run(run.id)
    second = await digest_run(run.id)
    assert first is not None and second is not None
    assert first.id == second.id
    async with get_session_factory()() as session:
        count = len(
            (await session.execute(select(RunDigest).where(RunDigest.run_id == run.id))).all()
        )
    assert count == 1


async def test_digest_skips_running_runs() -> None:
    from app.orchestrator.runner import create_run

    await _enable()
    run = await create_run(None, "still running")
    assert await digest_run(run.id) is None


# ── rollups ──────────────────────────────────────────────────────────


async def test_rollup_covers_all_digests() -> None:
    await _enable()
    run1 = await _completed_run("first task about databases", "did the first task")
    await digest_run(run1.id)
    run2 = await _completed_run(
        "second task about backups", "did the second task", run1.conversation_id
    )
    await digest_run(run2.id)
    rollup = await update_rollup(run1.conversation_id)
    assert rollup.runs_covered == 2
    assert "databases" in rollup.text and "backups" in rollup.text


async def test_process_run_pipeline_end_to_end() -> None:
    await _enable()
    run = await _completed_run("pipeline check task about kubernetes", "cluster healthy")
    await process_run(run.id)
    async with get_session_factory()() as session:
        digest = (
            await session.execute(select(RunDigest).where(RunDigest.run_id == run.id))
        ).scalar_one()
        rollup = await session.get(ConversationRollup, run.conversation_id)
    assert "kubernetes" in digest.text
    assert rollup is not None and rollup.runs_covered == 1


async def test_runner_completion_hook_creates_digest(client: AsyncClient) -> None:
    """End-to-end: a chat run completing fires the post-run pipeline."""
    import asyncio

    from app.llm import fake as fake_llm

    await _enable()
    await _set(default_model="fake:scripted")
    fake_llm.push_ai('{"entries": [], "direct_answer": "hello there", "no_confident_match": false}')
    resp = await client.post("/api/v1/chat", json={"message": "say hello please"})
    run_id = resp.json()["run_id"]
    for _ in range(80):
        status = (await client.get(f"/api/v1/runs/{run_id}")).json()["status"]
        if status not in {"running", "paused_hitl"}:
            break
        await asyncio.sleep(0.25)
    from app.memory.scheduler import drain

    await drain()
    async with get_session_factory()() as session:
        digest = (
            await session.execute(select(RunDigest).where(RunDigest.run_id == run_id))
        ).scalar_one_or_none()
    assert digest is not None, "post-run pipeline did not produce a digest"


async def test_runner_hook_noop_when_memory_disabled(client: AsyncClient) -> None:
    import asyncio

    from app.llm import fake as fake_llm

    await _set(default_model="fake:scripted")
    fake_llm.push_ai('{"entries": [], "direct_answer": "ok", "no_confident_match": false}')
    resp = await client.post("/api/v1/chat", json={"message": "quick check"})
    run_id = resp.json()["run_id"]
    for _ in range(80):
        status = (await client.get(f"/api/v1/runs/{run_id}")).json()["status"]
        if status not in {"running", "paused_hitl"}:
            break
        await asyncio.sleep(0.25)
    from app.memory.scheduler import drain

    await drain()
    async with get_session_factory()() as session:
        rows = (await session.execute(select(RunDigest))).all()
    assert rows == []


# ── episodic recall ──────────────────────────────────────────────────


async def test_recall_digests_cross_conversation_excludes_current() -> None:
    await _enable()
    run_a = await _completed_run(
        "research the langgraph checkpointing model", "checkpointing uses threads"
    )
    await digest_run(run_a.id)
    hits_elsewhere = await recall_digests("langgraph checkpointing", k=3)
    assert hits_elsewhere and hits_elsewhere[0][0].run_id == run_a.id
    hits_same = await recall_digests(
        "langgraph checkpointing", k=3, exclude_conversation_id=run_a.conversation_id
    )
    assert hits_same == []


# ── the injection block ──────────────────────────────────────────────


async def test_block_empty_when_disabled() -> None:
    block, stats = await build_memory_block("anything", conversation_id=None, surface="planner")
    assert block == "" and stats.tokens == 0


async def test_block_contains_memories_episodes_and_abstention() -> None:
    await _enable()
    await remember(
        text="the user's favorite database is postgres", kind="preference", source="user_stated"
    )
    run = await _completed_run("compare postgres and mysql for the project", "postgres won")
    await digest_run(run.id)
    async with get_session_factory()() as session:
        other = Conversation()
        session.add(other)
        await session.commit()
        other_id = other.id
    block, stats = await build_memory_block(
        "which database does the user prefer", conversation_id=other_id, surface="planner"
    )
    assert "<remembered_context>" in block
    assert "never invent a remembered fact" in block
    assert "favorite database is postgres" in block
    assert "Similar past episodes" in block
    assert stats.memories >= 1 and stats.episodes >= 1 and stats.tokens > 0


async def test_block_pinned_section_and_budget() -> None:
    await _enable()
    row = await remember(text="the user's name is Mahesh", kind="fact", source="user_stated")
    async with get_session_factory()() as session:
        from app.models import Memory

        fresh = await session.get(Memory, row.id)
        assert fresh is not None
        fresh.pinned = True
        await session.commit()
    for i in range(12):
        await remember(
            text=f"verbose fact number {i} about the alpha project with plenty of words "
            f"padding the token budget calculation for clipping tests",
            kind="fact",
            source="user_stated",
        )
    await _set(memory_injection_budget_tokens=60, memory_pinned_budget_tokens=40)
    block, stats = await build_memory_block(
        "alpha project facts", conversation_id=None, surface="planner"
    )
    assert "Pinned profile" in block and "Mahesh" in block
    assert stats.tokens <= 60 + 40 + 120  # sections + template overhead stays bounded
    assert stats.memories <= 3  # the 60-token budget clips hard


async def test_block_abstains_below_floor() -> None:
    await _enable()
    await remember(text="the sprint retro is on fridays", kind="fact", source="user_stated")
    await _set(memory_score_floor=0.99)
    block, stats = await build_memory_block(
        "zorblax gribble unrelated nonsense", conversation_id=None, surface="planner"
    )
    assert block == "" and stats.memories == 0


# ── planner + agentic + direct surfaces ──────────────────────────────


async def test_planner_prompt_carries_memory_block() -> None:
    from app.orchestrator.planner import build_planner_prompt

    summaries: dict[str, Any] = {"sub_agent_cards": [], "direct_capabilities": []}
    prompt = build_planner_prompt(
        "do the thing", "", summaries, 6, "<remembered_context>X</remembered_context>"
    )
    assert "<remembered_context>X</remembered_context>" in prompt
    bare = build_planner_prompt("do the thing", "", summaries, 6)
    assert "<remembered_context>" not in bare


async def test_include_memories_flag_rules(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/chat", json={"message": "hi", "include_memories": True})
    assert resp.status_code == 422
    assert "include_memories" in resp.text


async def test_direct_run_include_memories_composes_block(client: AsyncClient) -> None:
    import asyncio

    from app.llm import fake as fake_llm

    await _enable()
    await remember(
        text="the user's preferred greeting is 'howdy'", kind="preference", source="user_stated"
    )
    # a pinned direct run against the seeded research-concierge
    await client.post("/api/v1/seed/reload")
    agents = (await client.get("/api/v1/sub-agents")).json()
    target = next(a for a in agents if a["direct_exposure"] and a["status"] == "active")

    conv = (
        await client.post("/api/v1/chat", json={"message": "warmup"})
    ).json()  # creates conversation; fake needs a script
    # let the warmup run settle (it may fail without a script — fine)
    await asyncio.sleep(0.5)

    fake_llm.push_ai("worker output referencing the remembered greeting")
    fake_llm.push_ai("aggregated")
    resp = await client.post(
        "/api/v1/chat",
        json={
            "message": "greet me the way I like",
            "conversation_id": conv["conversation_id"],
            "target_sub_agent_id": target["id"],
            "include_memories": True,
        },
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    for _ in range(80):
        detail = (await client.get(f"/api/v1/runs/{run_id}")).json()
        if detail["status"] not in {"running", "paused_hitl"}:
            break
        await asyncio.sleep(0.25)
    assert detail["include_memories"] is True


# ── advisory job locks (M17 scaffolding) ─────────────────────────────


async def test_job_lock_exclusive_across_sessions() -> None:
    factory = get_session_factory()
    async with factory() as s1, factory() as s2:
        assert await acquire_job_lock(s1, 7) is True
        assert await acquire_job_lock(s2, 7) is False
        await release_job_lock(s1, 7)
        assert await acquire_job_lock(s2, 7) is True
        await release_job_lock(s2, 7)


async def test_block_approved_instructions_get_their_own_section() -> None:
    await _enable()
    await remember(
        text="always mention the runbook when deploys come up",
        kind="instruction",
        source="user_stated",
    )
    await remember(text="the deploy runbook lives in the wiki", kind="fact", source="user_stated")
    block, _ = await build_memory_block(
        "how should I handle the deploy", conversation_id=None, surface="planner"
    )
    assert "Approved standing instructions" in block
    assert "always mention the runbook" in block
    # facts stay under the data fence, not the instructions header
    assert block.index("never invent a remembered fact") < block.index("Approved standing")
