"""Procedural learning + lifecycle jobs (spec §16.5/16.2 — M16/M17)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.memory import remember
from app.memory.lifecycle import (
    contradiction_sweep,
    decay_sweep,
    reflection,
    reset_job_clock,
    run_due_jobs,
)
from app.memory.procedural import (
    PROPOSAL_PREFIX,
    exemplar_block,
    harvest_exemplar,
    mine_fallback_skills,
    recall_exemplars,
    update_routing_stats,
    vote_exemplars,
)
from app.models import Memory, PlanExemplar, RoutingStat, Run, RunStep, Skill
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable_procedural() -> None:
    await _set(
        memory_enabled=True,
        procedural_learning_enabled=True,
        embedding_model="fake:scripted",
    )


async def _finished_run(
    message: str,
    *,
    status: str = "completed",
    mode: str = "graph",
    rungs: list[tuple[str, str]] | None = None,
    denied: bool = False,
    tool_keys: list[str] | None = None,
    fallback: bool = False,
) -> Run:
    from app.orchestrator.runner import create_run

    run = await create_run(None, message, mode=mode)
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = status
        row.final_answer = "done"
        row.finished_at = datetime.now(UTC)
        for rung, entity in rungs or []:
            session.add(
                RunStep(
                    run_id=run.id,
                    step_type="route",
                    status="completed",
                    output={
                        "rung": rung,
                        "resolved_to": {"entity_id": None, "entity_name": entity},
                    },
                )
            )
        if denied:
            session.add(
                RunStep(
                    run_id=run.id,
                    step_type="hitl",
                    status="completed",
                    output={"status": "denied", "note": "no"},
                )
            )
        for key in tool_keys or []:
            session.add(
                RunStep(run_id=run.id, step_type="tool_call", status="completed", node_id=key)
            )
        await session.commit()
        await session.refresh(row)
    if fallback:
        from app.memory.episodic import digest_run

        await digest_run(run.id)
    return run


# ── routing stats ────────────────────────────────────────────────────


async def test_routing_stats_fold_runs() -> None:
    await _enable_procedural()
    r1 = await _finished_run("task one", rungs=[("custom_sub_agent", "research-concierge")])
    r2 = await _finished_run(
        "task two", status="failed", rungs=[("custom_sub_agent", "research-concierge")]
    )
    await update_routing_stats(r1.id)
    await update_routing_stats(r2.id)
    async with get_session_factory()() as session:
        stat = (
            await session.execute(
                select(RoutingStat).where(
                    RoutingStat.capability_key == "custom_sub_agent:research-concierge"
                )
            )
        ).scalar_one()
    assert stat.runs_total == 2
    assert stat.runs_completed == 1
    assert stat.runs_failed == 1


# ── exemplars: harvest, votes, recall, block ─────────────────────────


async def test_exemplar_harvested_only_from_positive_runs() -> None:
    await _enable_procedural()
    good = await _finished_run(
        "summarize the site notes file", rungs=[("custom_sub_agent", "site-analyst")]
    )
    denied = await _finished_run(
        "summarize the other file",
        rungs=[("custom_sub_agent", "site-analyst")],
        denied=True,
    )
    failed = await _finished_run(
        "summarize a third file", status="failed", rungs=[("custom_sub_agent", "site-analyst")]
    )
    assert await harvest_exemplar(good.id) is not None
    assert await harvest_exemplar(denied.id) is None
    assert await harvest_exemplar(failed.id) is None
    assert await harvest_exemplar(good.id) is not None  # idempotent
    async with get_session_factory()() as session:
        count = len((await session.execute(select(PlanExemplar))).all())
    assert count == 1


async def test_exemplar_votes_retire_at_zero() -> None:
    await _enable_procedural()
    run = await _finished_run("votable task", rungs=[("direct_skill", "notes-formatter")])
    ex = await harvest_exemplar(run.id)
    assert ex is not None and ex.votes == 1
    await vote_exemplars([ex.id], success=True)
    async with get_session_factory()() as session:
        fresh = await session.get(PlanExemplar, ex.id)
    assert fresh is not None and fresh.votes == 2 and fresh.status == "active"
    await vote_exemplars([ex.id], success=False)
    await vote_exemplars([ex.id], success=False)
    async with get_session_factory()() as session:
        fresh = await session.get(PlanExemplar, ex.id)
    assert fresh is not None and fresh.votes == 0 and fresh.status == "retired"
    assert await recall_exemplars("votable task") == []  # retired ⇒ invisible


async def test_exemplar_block_gated_and_formatted() -> None:
    block, ids = await exemplar_block("anything")  # everything off
    assert block == "" and ids == []
    await _enable_procedural()
    run = await _finished_run(
        "research the langgraph framework on the web",
        rungs=[("custom_sub_agent", "research-concierge")],
    )
    await harvest_exemplar(run.id)
    block, ids = await exemplar_block("research the langgraph framework thoroughly")
    assert "Similar past asks" in block
    assert "custom_sub_agent(research-concierge)" in block
    assert len(ids) == 1


# ── fallback mining ──────────────────────────────────────────────────


async def _seed_tool(tool_key: str) -> None:
    from app.models import Tool

    async with get_session_factory()() as session:
        session.add(
            Tool(
                name=tool_key,
                description="test tool",
                kind="mcp",
                source="static",
                tool_name=tool_key.split(".")[-1],
                tool_key=tool_key,
            )
        )
        await session.commit()


async def test_fallback_mining_creates_inactive_proposal() -> None:
    await _enable_procedural()
    await _seed_tool("filesystem.read_file")
    for i in range(3):
        await _finished_run(
            f"compute the sha-256 checksum digest of configuration string number {i}",
            rungs=[("fallback", "full-catalog fallback")],
            tool_keys=["filesystem.read_file"],
            fallback=True,
        )
    proposals = await mine_fallback_skills()
    assert len(proposals) == 1
    async with get_session_factory()() as session:
        skill = (
            await session.execute(select(Skill).where(Skill.name == proposals[0]))
        ).scalar_one()
        await session.refresh(skill, ["tools"])
        tool_keys = {t.tool_key for t in skill.tools}
    assert skill.status == "inactive"  # human review activates
    assert skill.source == "dynamic"
    assert skill.description.startswith(PROPOSAL_PREFIX)
    assert tool_keys == {"filesystem.read_file"}
    # idempotent: the same cluster does not propose twice
    assert await mine_fallback_skills() == []


async def test_fallback_mining_needs_recurrence() -> None:
    await _enable_procedural()
    await _finished_run(
        "a one-off uncovered ask about quantum jellyfish",
        rungs=[("fallback", "full-catalog fallback")],
        tool_keys=["filesystem.read_file"],
        fallback=True,
    )
    assert await mine_fallback_skills() == []


# ── lifecycle: decay, reflection, contradiction ──────────────────────


async def test_decay_expires_stale_unpinned_memories() -> None:
    await _set(memory_enabled=True, embedding_model="fake:scripted")
    stale = await remember(text="an old fact nobody accessed", kind="fact", source="user_stated")
    pinned = await remember(text="a pinned identity fact", kind="fact", source="user_stated")
    fresh = await remember(text="a fresh fact", kind="fact", source="user_stated")
    async with get_session_factory()() as session:
        srow = await session.get(Memory, stale.id)
        prow = await session.get(Memory, pinned.id)
        assert srow is not None and prow is not None
        srow.last_accessed_at = datetime.now(UTC) - timedelta(days=400)
        srow.importance = 3
        prow.last_accessed_at = datetime.now(UTC) - timedelta(days=400)
        prow.pinned = True
        await session.commit()
    expired = await decay_sweep()
    assert expired == 1
    async with get_session_factory()() as session:
        srow = await session.get(Memory, stale.id)
        prow = await session.get(Memory, pinned.id)
        frow = await session.get(Memory, fresh.id)
    assert srow is not None and srow.status == "expired"
    assert prow is not None and prow.status == "active"  # pinned immune
    assert frow is not None and frow.status == "active"


async def test_reflection_trigger_and_evidence_citations() -> None:
    await _set(
        memory_enabled=True,
        memory_reflection_enabled=True,
        embedding_model="fake:scripted",
        memory_extraction_model="fake:scripted",
    )
    for i in range(20):
        await remember(
            text=f"observation number {i} about the user's postgres-heavy workflow",
            kind="fact",
            source="user_stated",
            importance=8,
        )
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "ReflectionOutput",
                "args": {
                    "insights": [
                        {
                            "text": "the user's work centers on postgres operations",
                            "kind": "fact",
                            "importance": 7,
                            "evidence": [1, 2, 3],
                        },
                        {
                            "text": "an uncited insight that must be refused",
                            "kind": "fact",
                            "importance": 5,
                            "evidence": [],
                        },
                    ]
                },
                "id": "r",
            }
        ],
    )
    written = await reflection()
    assert written == 1
    async with get_session_factory()() as session:
        inferred = (
            await session.execute(select(Memory).where(Memory.source == "inferred"))
        ).scalar_one()
    assert "postgres operations" in inferred.text
    assert len((inferred.payload or {}).get("evidence", [])) == 3


async def test_reflection_below_trigger_is_noop() -> None:
    await _set(memory_enabled=True, memory_reflection_enabled=True, embedding_model="fake:scripted")
    await remember(text="one lonely fact", kind="fact", source="user_stated", importance=5)
    assert await reflection() == 0


async def test_contradiction_sweep_quarantines_duplicate_entity_keys() -> None:
    await _set(memory_enabled=True, embedding_model="fake:scripted")
    first = await remember(
        text="the deploy branch is main",
        kind="fact",
        source="user_stated",
        entity_key="deploy.branch",
    )
    # simulate drift: a second ACTIVE row for the same entity_key
    async with get_session_factory()() as session:
        session.add(
            Memory(
                text="the deploy branch is release",
                kind="fact",
                scope="global",
                source="user_stated",
                status="active",
                entity_key="deploy.branch",
                importance=5,
                confidence=0.9,
            )
        )
        await session.commit()
    quarantined = await contradiction_sweep()
    assert quarantined == 1
    async with get_session_factory()() as session:
        frow = await session.get(Memory, first.id)
        rows = list(
            (
                await session.execute(select(Memory).where(Memory.entity_key == "deploy.branch"))
            ).scalars()
        )
    assert frow is not None and frow.status == "active"  # oldest validity kept
    statuses = sorted(m.status for m in rows)
    assert statuses == ["active", "quarantined"]


async def test_run_due_jobs_respects_master_switch_and_locks() -> None:
    reset_job_clock()
    assert await run_due_jobs() == {}  # memory off ⇒ nothing runs
    await _set(memory_enabled=True, embedding_model="fake:scripted")
    reset_job_clock()
    results = await run_due_jobs()
    assert "decay" in results and "contradict" in results
    # immediately after, nothing is due
    assert await run_due_jobs() == {}
