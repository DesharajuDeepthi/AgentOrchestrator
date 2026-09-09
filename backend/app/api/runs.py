"""Runs API (spec §4): list/detail with ordered steps, cancel, retry, delete,
history purge."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep
from app.auth import owns_row, scope_to_user
from app.models import Run, RunStep
from app.orchestrator.runner import cancel_run, forget_run_events, retry_run

router = APIRouter(prefix="/runs", tags=["runs"])

# LangGraph checkpointer tables (owned by AsyncPostgresSaver.setup(), outside
# app metadata). Checkpoint rows ride the run lifecycle: the orchestrator
# thread is the run id, worker threads are "run_id:entry_id" — so deleting a
# run deletes its checkpoints (spec §8.7: purge leaves no run residue).
_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


async def _purge_checkpoints(session: AsyncSession, run_id: UUID | None = None) -> None:
    for table in _CHECKPOINT_TABLES:
        exists = await session.execute(text("SELECT to_regclass(:t)"), {"t": table})
        if exists.scalar_one_or_none() is None:
            continue  # saver has not created its tables yet (e.g. no run ever)
        if run_id is None:
            await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed table names
        else:
            await session.execute(
                text(
                    f"DELETE FROM {table} "  # noqa: S608 - fixed table names
                    "WHERE thread_id = :thread OR thread_id LIKE :prefix"
                ),
                {"thread": str(run_id), "prefix": f"{run_id}:%"},
            )


def _step_out(step: RunStep) -> dict[str, Any]:
    return {
        "id": str(step.id),
        "parent_step_id": str(step.parent_step_id) if step.parent_step_id else None,
        "sub_agent_id": str(step.sub_agent_id) if step.sub_agent_id else None,
        "node_id": step.node_id,
        "step_type": step.step_type,
        "input": step.input,
        "output": step.output,
        "model": step.model,
        "input_tokens": step.input_tokens,
        "output_tokens": step.output_tokens,
        "status": step.status,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "finished_at": step.finished_at.isoformat() if step.finished_at else None,
        "error": step.error,
    }


def _run_out(run: Run, with_steps: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "chat_message": run.chat_message,
        "status": run.status,
        "orchestrator_mode": run.orchestrator_mode,
        "target_sub_agent_id": str(run.target_sub_agent_id) if run.target_sub_agent_id else None,
        "include_history_summary": run.include_history_summary,
        "include_memories": run.include_memories,
        # §17.4 ambient provenance — None for interactive runs
        "trigger": run.trigger,
        "plan": run.plan,
        "snapshot": run.snapshot,
        "final_answer": run.final_answer,
        "answer_ui": run.answer_ui,
        "charts": run.charts,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "total_input_tokens": run.total_input_tokens,
        "total_output_tokens": run.total_output_tokens,
    }
    if with_steps:
        data["steps"] = [_step_out(s) for s in run.steps]
    return data


@router.get("")
async def list_runs(session: SessionDep, routine_id: UUID | None = None) -> list[dict[str, Any]]:
    query = scope_to_user(select(Run).order_by(Run.started_at.desc()), Run)
    if routine_id is not None:
        # §18.5: the routine drawer's run history — trigger provenance match
        query = query.where(Run.trigger["routine_id"].astext == str(routine_id))
    runs = list((await session.execute(query)).scalars())
    return [_run_out(r) for r in runs]


@router.delete("", status_code=204)
async def purge_runs(session: SessionDep) -> None:
    """Run-history purge (spec §8.7)."""
    for run_id in (await session.execute(select(Run.id))).scalars():
        forget_run_events(run_id)
    await session.execute(delete(RunStep))
    await session.execute(delete(Run))
    await _purge_checkpoints(session)
    await session.commit()


@router.get("/{run_id}")
async def get_run(run_id: UUID, session: SessionDep) -> dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None or not owns_row(run):
        raise HTTPException(status_code=404, detail="run not found")
    return _run_out(run, with_steps=True)


@router.post("/{run_id}/cancel")
async def cancel(run_id: UUID) -> dict[str, Any]:
    try:
        await cancel_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "cancelled"}


@router.post("/{run_id}/retry", status_code=201)
async def retry(run_id: UUID) -> dict[str, Any]:
    try:
        new_run = await retry_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": str(new_run.id), "conversation_id": str(new_run.conversation_id)}


@router.delete("/{run_id}", status_code=204)
async def delete_run(run_id: UUID, session: SessionDep) -> None:
    run = await session.get(Run, run_id)
    if run is None or not owns_row(run):
        raise HTTPException(status_code=404, detail="run not found")
    if run.status == "running":
        raise HTTPException(status_code=409, detail="cancel the run before deleting it")
    forget_run_events(run_id)
    await session.execute(delete(RunStep).where(RunStep.run_id == run_id))
    await session.delete(run)
    await _purge_checkpoints(session, run_id)
    await session.commit()
