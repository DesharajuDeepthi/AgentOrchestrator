"""M46 — the §16.2 embedding backfill job, closing the last spec-vs-code
gap: `MemoryEmbedding` is a side-table keyed by (row, model) so "a model
switch re-embeds in the background and flips" — but until now only the
registry had a backfill; memory rows written under the old model (or
whose write-through embed failed) stayed invisible to vector retrieval
under the new key. Found live in stage 32, recorded as the known gap.

Deliberate non-goal, asserted here: tombstones are NOT backfillable —
they keep no text by design, so pre-switch tombstones degrade to
hash+anchor matching permanently. Privacy over recall, by choice.
"""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import get_session_factory
from app.memory.lifecycle import embedding_backfill, reset_job_clock, run_due_jobs
from app.memory.store import remember
from app.models import (
    Conversation,
    Memory,
    MemoryEmbedding,
    MemoryTombstone,
    PlanExemplar,
    Run,
    RunDigest,
)
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _embedding_keys(ref_id: Any, table_ref: str) -> set[str]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MemoryEmbedding.model_key).where(
                    MemoryEmbedding.ref_id == ref_id,
                    MemoryEmbedding.table_ref == table_ref,
                )
            )
        ).scalars()
        return set(rows)


async def _bare_memory(text: str, status: str = "active") -> Memory:
    """A memory row with NO embedding rows — a write-through failure."""
    async with get_session_factory()() as session:
        row = Memory(text=text, kind="fact", scope="global", source="user_stated", status=status)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def _run_row() -> Run:
    async with get_session_factory()() as session:
        conv = Conversation(title="m46 probe")
        session.add(conv)
        await session.flush()
        run = Run(
            conversation_id=conv.id,
            chat_message="m46",
            status="completed",
            trigger={"kind": "test"},
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
    return run


class TestBackfill:
    async def test_no_embedding_model_is_a_noop(self, client: Any) -> None:
        await _set(embedding_model=None)
        await _bare_memory("orphan fact")
        assert await embedding_backfill() == 0
        async with get_session_factory()() as session:
            assert (await session.execute(select(MemoryEmbedding))).scalars().first() is None

    async def test_model_switch_backfills_under_the_new_key_and_keeps_the_old(
        self, client: Any
    ) -> None:
        """The §16.1 contract verbatim: old and new coexist; retrieval
        flips because it queries the ACTIVE key."""
        await _set(embedding_model="fake:scripted")
        row = await remember(
            text="deploy target is cluster-blue", kind="fact", source="user_stated"
        )
        assert await _embedding_keys(row.id, "memories") == {"fake:scripted@64"}
        await _set(embedding_model="fake:other")
        assert await embedding_backfill() >= 1
        assert await _embedding_keys(row.id, "memories") == {
            "fake:scripted@64",
            "fake:other@64",
        }

    async def test_write_through_failure_is_repaired(self, client: Any) -> None:
        await _set(embedding_model="fake:scripted")
        row = await _bare_memory("embed failed at write time")
        assert await embedding_backfill() >= 1
        assert await _embedding_keys(row.id, "memories") == {"fake:scripted@64"}

    async def test_second_pass_is_zero(self, client: Any) -> None:
        await _set(embedding_model="fake:scripted")
        await _bare_memory("once only")
        assert await embedding_backfill() >= 1
        assert await embedding_backfill() == 0

    async def test_covers_digests_and_exemplars(self, client: Any) -> None:
        await _set(embedding_model="fake:scripted")
        run = await _run_row()
        async with get_session_factory()() as session:
            digest = RunDigest(
                run_id=run.id, conversation_id=run.conversation_id, text="did things"
            )
            exemplar = PlanExemplar(
                run_id=run.id, task_text="ship the report", mode="graph", trace={"entries": []}
            )
            session.add_all([digest, exemplar])
            await session.commit()
            await session.refresh(digest)
            await session.refresh(exemplar)
        assert await embedding_backfill() >= 2
        assert await _embedding_keys(digest.id, "run_digests") == {"fake:scripted@64"}
        assert await _embedding_keys(exemplar.id, "plan_exemplars") == {"fake:scripted@64"}

    async def test_dead_rows_are_not_re_embedded(self, client: Any) -> None:
        """Expired/superseded memories and retired exemplars never surface
        in retrieval — re-embedding them would be silent token spend."""
        await _set(embedding_model="fake:scripted")
        expired = await _bare_memory("stale", status="expired")
        run = await _run_row()
        async with get_session_factory()() as session:
            retired = PlanExemplar(
                run_id=run.id, task_text="old plan", mode="graph", trace={}, status="retired"
            )
            session.add(retired)
            await session.commit()
            await session.refresh(retired)
        await embedding_backfill()
        assert await _embedding_keys(expired.id, "memories") == set()
        assert await _embedding_keys(retired.id, "plan_exemplars") == set()

    async def test_quarantined_memories_are_covered(self, client: Any) -> None:
        """Approval must make a quarantined row instantly retrievable —
        so it is embedded while still in review."""
        await _set(embedding_model="fake:scripted")
        row = await _bare_memory("under review", status="quarantined")
        await embedding_backfill()
        assert await _embedding_keys(row.id, "memories") == {"fake:scripted@64"}

    async def test_tombstones_stay_stale_by_design(self, client: Any) -> None:
        await _set(embedding_model="fake:scripted")
        async with get_session_factory()() as session:
            stone = MemoryTombstone(
                scope="global",
                kind="fact",
                source="extracted",
                text_hash="a" * 64,
                token_hashes=[],
                embedding=None,
                model_key=None,
            )
            session.add(stone)
            await session.commit()
            await session.refresh(stone)
        await embedding_backfill()
        async with get_session_factory()() as session:
            fresh = await session.get(MemoryTombstone, stone.id)
            assert fresh is not None and fresh.embedding is None and fresh.model_key is None

    async def test_rides_the_periodic_job_loop(self, client: Any) -> None:
        await _set(memory_enabled=True, embedding_model="fake:scripted")
        await _bare_memory("periodic pickup")
        reset_job_clock()
        try:
            results = await run_due_jobs()
            assert results.get("backfill", 0) >= 1
        finally:
            await _set(memory_enabled=False)

    async def test_batch_cap_bounds_one_pass(self, client: Any) -> None:
        await _set(embedding_model="fake:scripted")
        for i in range(5):
            await _bare_memory(f"bulk row {i} {uuid4()}")
        assert await embedding_backfill(limit=3) == 3
        assert await embedding_backfill(limit=100) == 2
