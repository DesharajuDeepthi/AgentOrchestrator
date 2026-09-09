"""LangSmith results publishing (spec §15): dataset + example-level
feedback when `langsmith_enabled` is on AND LANGSMITH_API_KEY is set
(env-only — never DB, never UI). Returns the dataset URL, or None when
publishing is off/unconfigured."""

import asyncio
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from app.db import get_session_factory
from app.models import EvalCase, EvalDataset, EvalResult, EvalRun

logger = structlog.get_logger("evals")


async def publish_eval_run(eval_run_id: UUID) -> str | None:
    from app.config import get_config
    from app.registry_cache import get_cache

    cache = get_cache()
    if not bool(await cache.setting("langsmith_enabled")):
        return None
    api_key = get_config().langsmith_api_key
    if not api_key:
        return None
    endpoint = str(await cache.setting("langsmith_endpoint") or "https://api.smith.langchain.com")

    async with get_session_factory()() as session:
        eval_run = await session.get(EvalRun, eval_run_id)
        assert eval_run is not None
        dataset = await session.get(EvalDataset, eval_run.dataset_id)
        assert dataset is not None
        results = list(
            (
                await session.execute(
                    select(EvalResult).where(EvalResult.eval_run_id == eval_run_id)
                )
            ).scalars()
        )
        cases = {
            c.id: c
            for c in (
                await session.execute(select(EvalCase).where(EvalCase.dataset_id == dataset.id))
            ).scalars()
        }

    def _publish() -> str | None:
        from langsmith import Client

        client = Client(api_url=endpoint, api_key=api_key)
        ds_name = f"concierge-eval-{dataset.name}-{str(eval_run_id)[:8]}"
        ls_dataset = client.create_dataset(ds_name)
        inputs: list[dict[str, Any]] = []
        outputs: list[dict[str, Any]] = []
        for r in results:
            case = cases.get(r.case_id)
            inputs.append({"input": case.input if case else ""})
            outputs.append(
                {
                    "expected": case.expected if case else "",
                    "answer": r.answer,
                    "passed": r.passed,
                    "score": r.score,
                    "reason": r.grader_reason,
                }
            )
        client.create_examples(dataset_id=ls_dataset.id, inputs=inputs, outputs=outputs)
        return str(getattr(ls_dataset, "url", None) or f"{endpoint}/datasets/{ls_dataset.id}")

    url = await asyncio.to_thread(_publish)
    logger.info("eval_published", tier="evals", kind="publish", url=url)
    return url
