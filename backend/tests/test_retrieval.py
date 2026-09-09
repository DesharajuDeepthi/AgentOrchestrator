"""Progressive-disclosure retrieval (spec §7.4): ranker units + catalog gate."""

import pytest
from httpx import AsyncClient

from app.db import get_session_factory
from app.orchestrator.context import RunContext, set_run_context
from app.registry_cache import get_cache
from app.retrieval import (
    apply_retrieval,
    bm25_scores,
    catalog_footer,
    cosine,
    rank_records,
    rrf_fuse,
)
from app.settings_store import update_settings
from tests.factory_helpers import create_tool


class TestRanker:
    def test_bm25_prefers_matching_doc(self) -> None:
        docs = ["fetch web pages over http", "format meeting notes", "read filesystem files"]
        scores = bm25_scores("fetch a web page", docs)
        assert scores[0] > scores[1] and scores[0] > scores[2]

    def test_cosine_basics(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert cosine([], [1.0]) == 0.0

    def test_rrf_fusion_rewards_agreement(self) -> None:
        # doc 2 tops both rankings — fusion must put it first
        fused = rrf_fuse([[2, 0, 1], [2, 1, 0]], 3)
        assert fused[0] == 2

    def test_rank_records_lexical_only(self) -> None:
        records = [
            {"name": "web-fetch", "description": "fetch pages from the web"},
            {"name": "notes", "description": "format notes"},
        ]
        order = rank_records("fetch web page", records)
        assert order[0] == 0

    def test_rank_records_vector_breaks_lexical_tie(self) -> None:
        records = [
            {"name": "a", "description": "", "embedding": [1.0, 0.0]},
            {"name": "b", "description": "", "embedding": [0.0, 1.0]},
        ]
        order = rank_records("unrelated words", records, query_vec=[0.0, 1.0])
        assert order[0] == 1


class TestCatalogGate:
    async def _enable(self, threshold: int = 2, top_k: int = 2) -> None:
        async with get_session_factory()() as session:
            await update_settings(
                session,
                {
                    "retrieval_enabled": True,
                    "retrieval_threshold": threshold,
                    "retrieval_top_k": top_k,
                },
            )

    def _records(self, n: int) -> list[dict[str, str]]:
        return [{"id": f"r{i}", "name": f"rec-{i}", "description": f"topic {i}"} for i in range(n)]

    async def test_disabled_is_identity(self) -> None:
        records = self._records(50)
        out, dropped = await apply_retrieval(records, kind="tools", query="topic 3")
        assert out == records and dropped == 0

    async def test_below_threshold_is_identity(self) -> None:
        await self._enable(threshold=10)
        records = self._records(5)
        out, dropped = await apply_retrieval(records, kind="tools", query="topic 3")
        assert out == records and dropped == 0

    async def test_over_threshold_truncates_to_top_k(self) -> None:
        await self._enable(threshold=2, top_k=2)
        records = self._records(6)
        out, dropped = await apply_retrieval(records, kind="tools", query="topic 4")
        assert len(out) == 2 and dropped == 4
        assert any(r["id"] == "r4" for r in out)  # the lexical match survives

    async def test_pinned_ids_bypass_ranking(self) -> None:
        await self._enable(threshold=2, top_k=1)
        from uuid import uuid4

        ctx = RunContext(run_id=uuid4(), mode="graph", recorder=None, query_text="topic 0")
        ctx.pinned_ids.add("r5")
        set_run_context(ctx)
        records = self._records(6)
        out, dropped = await apply_retrieval(records, kind="skills")
        assert any(r["id"] == "r5" for r in out)  # pinned past ranking
        assert any(r["id"] == "r0" for r in out)  # ranked in via query_text

    async def test_no_query_is_identity(self) -> None:
        await self._enable(threshold=1, top_k=1)
        records = self._records(4)
        out, dropped = await apply_retrieval(records, kind="tools")
        assert out == records and dropped == 0

    def test_footer_wording(self) -> None:
        footer = catalog_footer("skills", 3, 40)
        assert "3 of 40" in footer and "use_full_catalog" in footer


class TestEmbeddingsPipeline:
    async def test_fake_embeddings_maintained_on_write(self, client: AsyncClient) -> None:
        """Write-path embed via the fake provider (spec §7.4 best-effort)."""
        async with get_session_factory()() as session:
            await update_settings(
                session,
                {"retrieval_enabled": True, "embedding_model": "fake:scripted"},
            )
        tool = await create_tool(direct_exposure=True)
        from app.retrieval import refresh_record_embedding

        assert await refresh_record_embedding("tools", str(tool.id)) is True
        record = await get_cache().tool_by_id(tool.id)
        assert record is not None and isinstance(record["embedding"], list)
        # unchanged text → no re-embed
        assert await refresh_record_embedding("tools", str(tool.id)) is False

    async def test_embedding_model_validation(self, client: AsyncClient) -> None:
        resp = await client.patch("/api/v1/settings", json={"embedding_model": "anthropic:x"})
        assert resp.status_code == 422
        assert "no embeddings API" in resp.text
        resp = await client.patch("/api/v1/settings", json={"embedding_model": "fake:scripted"})
        assert resp.status_code == 200
