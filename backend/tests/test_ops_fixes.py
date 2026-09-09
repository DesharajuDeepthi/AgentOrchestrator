"""Live log_level / otlp_endpoint settings (spec §5b/§10) and checkpoint
cleanup on run purge (spec §8.7)."""

import logging
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text

from app.db import get_checkpointer, get_session_factory
from app.models import Conversation, Run


class TestLiveObservabilitySettings:
    async def test_log_level_patch_applies_immediately(self, client: AsyncClient) -> None:
        from app.obs import configure_logging

        try:
            resp = await client.patch("/api/v1/settings", json={"log_level": "DEBUG"})
            assert resp.status_code == 200
            assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
            resp = await client.patch("/api/v1/settings", json={"log_level": "WARNING"})
            assert resp.status_code == 200
            assert logging.getLogger().getEffectiveLevel() == logging.WARNING
        finally:
            configure_logging("INFO")

    async def test_log_level_still_validated(self, client: AsyncClient) -> None:
        resp = await client.patch("/api/v1/settings", json={"log_level": "verbose"})
        assert resp.status_code == 422

    async def test_otlp_endpoint_patch_swaps_exporter(self, client: AsyncClient) -> None:
        from app.obs import apply_otlp_endpoint, otlp_endpoint_in_use

        try:
            resp = await client.patch(
                "/api/v1/settings", json={"otlp_endpoint": "http://collector:4318"}
            )
            assert resp.status_code == 200
            assert otlp_endpoint_in_use() == "http://collector:4318"
            # empty string disables export live
            resp = await client.patch("/api/v1/settings", json={"otlp_endpoint": ""})
            assert resp.status_code == 200
            assert otlp_endpoint_in_use() is None
        finally:
            apply_otlp_endpoint(None)


async def _make_run() -> Run:
    async with get_session_factory()() as session:
        conv = Conversation(title="t")
        session.add(conv)
        await session.flush()
        run = Run(
            conversation_id=conv.id,
            chat_message="hi",
            status="completed",
            orchestrator_mode="graph",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def _insert_checkpoint_rows(thread_ids: list[str]) -> None:
    """Minimal rows in the saver-owned tables (created via get_checkpointer)."""
    await get_checkpointer()
    async with get_session_factory()() as session:
        for thread in thread_ids:
            await session.execute(
                text(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                    "VALUES (:t, '', :cid, '{}'::jsonb, '{}'::jsonb)"
                ),
                {"t": thread, "cid": uuid4().hex},
            )
        await session.commit()


async def _checkpoint_threads() -> set[str]:
    async with get_session_factory()() as session:
        rows = await session.execute(text("SELECT DISTINCT thread_id FROM checkpoints"))
        return {r[0] for r in rows}


class TestCheckpointPurge:
    async def test_delete_run_removes_its_checkpoints_only(self, client: AsyncClient) -> None:
        doomed, kept = await _make_run(), await _make_run()
        # orchestrator thread (run id) + a worker thread (run_id:entry)
        await _insert_checkpoint_rows(
            [str(doomed.id), f"{doomed.id}:n1", str(kept.id), f"{kept.id}:n1"]
        )
        resp = await client.delete(f"/api/v1/runs/{doomed.id}")
        assert resp.status_code == 204
        remaining = await _checkpoint_threads()
        assert str(doomed.id) not in remaining
        assert f"{doomed.id}:n1" not in remaining
        assert {str(kept.id), f"{kept.id}:n1"} <= remaining

    async def test_purge_all_runs_removes_all_checkpoints(self, client: AsyncClient) -> None:
        run = await _make_run()
        await _insert_checkpoint_rows([str(run.id), f"{run.id}:n1"])
        resp = await client.delete("/api/v1/runs")
        assert resp.status_code == 204
        assert await _checkpoint_threads() == set()

    async def test_purge_without_checkpoint_tables_is_safe(self, client: AsyncClient) -> None:
        """to_regclass guard: purge succeeds even if the saver never ran."""
        async with get_session_factory()() as session:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await session.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await session.execute(text("DROP TABLE IF EXISTS checkpoint_migrations"))
            await session.commit()
        try:
            run = await _make_run()
            assert (await client.delete(f"/api/v1/runs/{run.id}")).status_code == 204
            assert (await client.delete("/api/v1/runs")).status_code == 204
        finally:
            # recreate the saver tables for whatever test runs next
            await (await get_checkpointer()).setup()
