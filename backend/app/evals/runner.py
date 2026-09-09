"""The eval batch runner (spec §15): admin-direct, sequential, on the
EXISTING run machinery — every case becomes an ordinary Run (is_eval=true)
whose HITL pauses are auto-approved, then the case grades against the
run's final answer. Config snapshots make every eval run reproducible."""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from app.db import get_session_factory
from app.evals.grade import grade_case
from app.models import EvalCase, EvalDataset, EvalResult, EvalRun, Run

logger = structlog.get_logger("evals")

CASE_TIMEOUT_S = 600.0
_POLL_S = 0.05


async def _target_snapshot(dataset: EvalDataset) -> dict[str, Any]:
    from app.registry_cache import get_cache

    cache = get_cache()
    if dataset.level == "skill":
        return dict(await cache.skill_by_id(str(dataset.target_id)) or {})
    return dict(await cache.sub_agent_by_id(str(dataset.target_id)) or {})


async def _await_run(run_id: UUID) -> Run:
    """Wait for the child run, auto-approving HITL gates (spec §15: eval
    mode auto-approves) until it reaches a terminal state."""
    from app.orchestrator.runner import RUNNING_TASKS, resume_run

    deadline = asyncio.get_event_loop().time() + CASE_TIMEOUT_S
    while True:
        task = RUNNING_TASKS.get(run_id)
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=CASE_TIMEOUT_S)
        async with get_session_factory()() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            status = run.status
        if status == "paused_hitl":
            logger.info("eval_hitl_auto_approve", tier="evals", kind="hitl", run_id=str(run_id))
            await resume_run(run_id, "approve", "eval mode auto-approve", None)
            await asyncio.sleep(_POLL_S)
            continue
        if status != "running":
            async with get_session_factory()() as session:
                final = await session.get(Run, run_id)
                assert final is not None
                return final
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"eval case run {run_id} exceeded {CASE_TIMEOUT_S}s")
        await asyncio.sleep(_POLL_S)


async def execute_eval_run(dataset_id: UUID, eval_run_id: UUID | None = None) -> EvalRun:
    """Run every case of a dataset sequentially; returns the finished
    EvalRun row (status 'completed', or 'failed' on a harness error)."""
    from app.orchestrator.graph_mode import load_settings_snapshot
    from app.orchestrator.runner import create_run, start_run_task

    async with get_session_factory()() as session:
        dataset = await session.get(EvalDataset, dataset_id)
        if dataset is None:
            raise ValueError(f"eval dataset {dataset_id} not found")
        cases = list(
            (
                await session.execute(
                    select(EvalCase)
                    .where(EvalCase.dataset_id == dataset_id)
                    .order_by(EvalCase.position)
                )
            ).scalars()
        )
        if eval_run_id is None:
            eval_run = EvalRun(dataset_id=dataset_id, total_cases=len(cases))
            session.add(eval_run)
        else:
            eval_run = await session.get(EvalRun, eval_run_id)  # type: ignore[assignment]
            assert eval_run is not None
            eval_run.total_cases = len(cases)
        settings = await load_settings_snapshot()
        eval_run.config_snapshot = {
            "settings": {
                k: v
                for k, v in settings.items()
                if isinstance(v, str | int | float | bool | type(None))
            },
            "target": await _target_snapshot(dataset),
            "level": dataset.level,
            "target_id": str(dataset.target_id),
        }
        await session.commit()
        await session.refresh(eval_run)
        run_row_id = eval_run.id

    passed = failed = errored = 0
    try:
        for case in cases:
            run = await create_run(
                None,
                case.input,
                mode="direct",
                target_sub_agent_id=dataset.target_id if dataset.level == "sub_agent" else None,
                is_eval=True,
                eval_skill_id=dataset.target_id if dataset.level == "skill" else None,
            )
            start_run_task(run.id)
            try:
                finished = await _await_run(run.id)
                answer = finished.final_answer or ""
                if finished.status != "completed":
                    verdict = {
                        "status": "error",
                        "passed": False,
                        "score": 0.0,
                        "reason": f"run {finished.status}: {finished.error or 'no answer'}",
                    }
                else:
                    verdict = await grade_case(
                        grader=case.grader,
                        answer=answer,
                        expected=case.expected,
                        judge_notes=case.judge_notes,
                    )
            except Exception as exc:  # noqa: BLE001 — a broken case never kills the batch
                answer = ""
                verdict = {
                    "status": "error",
                    "passed": False,
                    "score": 0.0,
                    "reason": f"harness error: {exc}"[:2000],
                }
            if verdict["status"] == "error":
                errored += 1
            elif verdict["passed"]:
                passed += 1
            else:
                failed += 1
            async with get_session_factory()() as session:
                session.add(
                    EvalResult(
                        eval_run_id=run_row_id,
                        case_id=case.id,
                        run_id=run.id,
                        status=str(verdict["status"]),
                        passed=bool(verdict["passed"]),
                        score=float(verdict["score"]),  # type: ignore[arg-type]
                        grader_reason=str(verdict["reason"]),
                        answer=answer[:20000],
                    )
                )
                row = await session.get(EvalRun, run_row_id)
                assert row is not None
                row.passed_cases, row.failed_cases, row.error_cases = passed, failed, errored
                await session.commit()
        langsmith_url = await _maybe_publish(run_row_id)
        async with get_session_factory()() as session:
            row = await session.get(EvalRun, run_row_id)
            assert row is not None
            row.status = "completed"
            row.langsmith_url = langsmith_url
            row.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            final_row = row
    except Exception as exc:  # noqa: BLE001 — harness failure is recorded, not raised
        logger.warning("eval_run_failed", tier="evals", kind="run", error=str(exc))
        async with get_session_factory()() as session:
            row = await session.get(EvalRun, run_row_id)
            assert row is not None
            row.status = "failed"
            row.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            final_row = row
    logger.info(
        "eval_run_finished",
        tier="evals",
        kind="run",
        status=final_row.status,
        passed=passed,
        failed=failed,
        errors=errored,
    )
    return final_row


async def _maybe_publish(eval_run_id: UUID) -> str | None:
    """LangSmith publishing (spec §15): dataset + results when
    langsmith_enabled and the env key exist; otherwise skipped silently —
    Postgres traces remain the full record either way."""
    try:
        from app.evals.publish import publish_eval_run

        return await publish_eval_run(eval_run_id)
    except Exception as exc:  # noqa: BLE001 — publishing must never fail the eval
        logger.warning("eval_publish_failed", tier="evals", kind="publish", error=str(exc))
        return None
