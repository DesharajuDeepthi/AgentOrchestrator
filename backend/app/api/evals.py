"""Evals API (spec §15 — M32): dataset upload (csv/xlsx), batch launch,
and graded results."""

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.evals.parse import EvalParseError, parse_eval_file
from app.models import EvalCase, EvalDataset, EvalResult, EvalRun, Skill, SubAgent


async def _require_evals_on() -> None:
    """M48 §3.7.1: the §15 surface answers to a switch. The feature is
    passive, so this removes surface area rather than changing behavior —
    datasets and prior runs survive a flip in either direction."""
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("evals_enabled")):
        raise HTTPException(409, "evals are disabled (evals_enabled=false)")


router = APIRouter(prefix="/evals", tags=["evals"], dependencies=[Depends(_require_evals_on)])

_RUN_TASKS: dict[UUID, asyncio.Task[Any]] = {}


def _dataset_out(d: EvalDataset, case_count: int, target_name: str | None) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "name": d.name,
        "level": d.level,
        "target_id": str(d.target_id),
        "target_name": target_name,
        "case_count": case_count,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


async def _target_name(session: Any, level: str, target_id: UUID) -> str | None:
    model = Skill if level == "skill" else SubAgent
    row = await session.get(model, target_id)
    return row.name if row is not None else None


@router.post("/datasets", status_code=201)
async def upload_dataset(
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    data = await file.read()
    try:
        parsed = parse_eval_file(file.filename or "upload.csv", data)
    except EvalParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    target_id = UUID(parsed["target_id"])
    target = await _target_name(session, parsed["level"], target_id)
    if target is None:
        raise HTTPException(422, f"no {parsed['level']} with id {target_id}")
    dataset = EvalDataset(
        name=name or (file.filename or "dataset").rsplit(".", 1)[0],
        level=parsed["level"],
        target_id=target_id,
    )
    session.add(dataset)
    await session.flush()
    for i, case in enumerate(parsed["cases"]):
        session.add(EvalCase(dataset_id=dataset.id, position=i, **case))
    await session.commit()
    await session.refresh(dataset)
    return _dataset_out(dataset, len(parsed["cases"]), target)


@router.get("/datasets")
async def list_datasets(session: SessionDep) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(select(EvalDataset).order_by(EvalDataset.created_at.desc()))
        ).scalars()
    )
    counts: dict[UUID, int] = {
        row[0]: int(row[1])
        for row in (
            await session.execute(
                select(EvalCase.dataset_id, func.count()).group_by(EvalCase.dataset_id)
            )
        ).all()
    }
    out = []
    for d in rows:
        out.append(
            _dataset_out(
                d, int(counts.get(d.id, 0)), await _target_name(session, d.level, d.target_id)
            )
        )
    return {"items": out}


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: UUID, session: SessionDep) -> dict[str, Any]:
    d = await session.get(EvalDataset, dataset_id)
    if d is None:
        raise HTTPException(404, "no such dataset")
    cases = list(
        (
            await session.execute(
                select(EvalCase)
                .where(EvalCase.dataset_id == dataset_id)
                .order_by(EvalCase.position)
            )
        ).scalars()
    )
    out = _dataset_out(d, len(cases), await _target_name(session, d.level, d.target_id))
    out["cases"] = [
        {
            "id": str(c.id),
            "input": c.input,
            "expected": c.expected,
            "judge_notes": c.judge_notes,
            "grader": c.grader,
        }
        for c in cases
    ]
    return out


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: UUID, session: SessionDep) -> None:
    d = await session.get(EvalDataset, dataset_id)
    if d is None:
        raise HTTPException(404, "no such dataset")
    await session.delete(d)
    await session.commit()


@router.post("/datasets/{dataset_id}/run", status_code=201)
async def start_eval_run(dataset_id: UUID, session: SessionDep) -> dict[str, Any]:
    from app.evals.runner import execute_eval_run

    d = await session.get(EvalDataset, dataset_id)
    if d is None:
        raise HTTPException(404, "no such dataset")
    eval_run = EvalRun(dataset_id=dataset_id)
    session.add(eval_run)
    await session.commit()
    await session.refresh(eval_run)
    task = asyncio.create_task(execute_eval_run(dataset_id, eval_run.id))
    _RUN_TASKS[eval_run.id] = task
    task.add_done_callback(lambda t: _RUN_TASKS.pop(eval_run.id, None))
    return {"id": str(eval_run.id), "status": eval_run.status}


def _eval_run_out(r: EvalRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "dataset_id": str(r.dataset_id),
        "status": r.status,
        "total_cases": r.total_cases,
        "passed_cases": r.passed_cases,
        "failed_cases": r.failed_cases,
        "error_cases": r.error_cases,
        "langsmith_url": r.langsmith_url,
        "config_snapshot": r.config_snapshot,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@router.get("/runs")
async def list_eval_runs(session: SessionDep, dataset_id: UUID | None = None) -> dict[str, Any]:
    query = select(EvalRun).order_by(EvalRun.started_at.desc()).limit(100)
    if dataset_id is not None:
        query = query.where(EvalRun.dataset_id == dataset_id)
    rows = list((await session.execute(query)).scalars())
    return {"items": [_eval_run_out(r) for r in rows]}


@router.get("/runs/{eval_run_id}")
async def get_eval_run(eval_run_id: UUID, session: SessionDep) -> dict[str, Any]:
    r = await session.get(EvalRun, eval_run_id)
    if r is None:
        raise HTTPException(404, "no such eval run")
    out = _eval_run_out(r)
    results = list(
        (
            await session.execute(select(EvalResult).where(EvalResult.eval_run_id == eval_run_id))
        ).scalars()
    )
    cases = {
        c.id: c
        for c in (
            await session.execute(select(EvalCase).where(EvalCase.dataset_id == r.dataset_id))
        ).scalars()
    }
    out["results"] = [
        {
            "id": str(res.id),
            "case_id": str(res.case_id),
            "input": cases[res.case_id].input if res.case_id in cases else "",
            "expected": cases[res.case_id].expected if res.case_id in cases else "",
            "grader": cases[res.case_id].grader if res.case_id in cases else "",
            "run_id": str(res.run_id) if res.run_id else None,
            "status": res.status,
            "passed": res.passed,
            "score": res.score,
            "reason": res.grader_reason,
            "answer": res.answer,
        }
        for res in results
    ]
    return out
