"""a2a_tasks bookkeeping (spec §19.5/§19.6).

One row per outbound remote task. Three jobs:
- adoption: a HITL resume replays the interrupted tool call, and the
  replayed call must ADOPT the open task for its (run_id, call_key)
  instead of re-sending — the §7.1 spin_worker replay contract applied
  to remote tasks;
- state mirror: the last observed remote state, the pending question,
  and the final result live here for the trace, the task drawer, and
  the M39 poller;
- parking: rows flipped to state='parked' are the poller's work queue.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db import get_session_factory
from app.models import A2A_OPEN_STATES, A2ATask


def call_key_for(tool_id: str, args: dict[str, Any]) -> str:
    """Deterministic key over (tool, canonical args) for replay adoption."""
    canonical = json.dumps({"tool": tool_id, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def find_open_task(run_id: UUID | None, call_key: str) -> A2ATask | None:
    if run_id is None:
        return None
    async with get_session_factory()() as db:
        row = (
            await db.execute(
                select(A2ATask)
                .where(
                    A2ATask.run_id == run_id,
                    A2ATask.call_key == call_key,
                    A2ATask.state.in_(A2A_OPEN_STATES),
                )
                .order_by(A2ATask.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row


async def record_task(
    *,
    remote_agent_id: UUID,
    run_id: UUID | None,
    call_key: str,
    remote_task_id: str | None,
    context_id: str | None,
    state: str,
    question: str | None = None,
) -> UUID:
    async with get_session_factory()() as db:
        row = A2ATask(
            remote_agent_id=remote_agent_id,
            run_id=run_id,
            call_key=call_key,
            remote_task_id=remote_task_id,
            context_id=context_id,
            state=state,
            question=question,
        )
        db.add(row)
        await db.commit()
        return row.id


async def update_task(
    task_id: UUID,
    *,
    state: str | None = None,
    question: str | None = None,
    result_text: str | None = None,
    error: str | None = None,
    parked: bool = False,
    delivered: bool | None = None,
) -> None:
    async with get_session_factory()() as db:
        row = await db.get(A2ATask, task_id)
        if row is None:
            return
        if state is not None:
            row.state = state
        row.question = question
        if result_text is not None:
            row.result = {"text": result_text}
        if error is not None:
            row.error = error
        if parked:
            row.state = "parked"
            row.parked_at = datetime.now(UTC)
        if delivered is not None:
            row.delivered = delivered
        await db.commit()


async def get_task(task_id: UUID) -> A2ATask | None:
    async with get_session_factory()() as db:
        return await db.get(A2ATask, task_id)


async def parked_tasks(limit: int = 50) -> list[A2ATask]:
    async with get_session_factory()() as db:
        rows = (
            await db.execute(
                select(A2ATask)
                .where(A2ATask.state == "parked", A2ATask.delivered.is_(False))
                .order_by(A2ATask.parked_at)
                .limit(limit)
            )
        ).scalars()
        return list(rows)


async def parked_count() -> int:
    from sqlalchemy import func

    async with get_session_factory()() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).where(
                        A2ATask.state == "parked", A2ATask.delivered.is_(False)
                    )
                )
            ).scalar_one()
        )
