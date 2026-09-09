"""Remote agents registry API (spec §19.2/§19.3/§19.6, §4).

Writes 409 while `a2a_enabled` is false (the §17 dark pattern); reads stay
200. Credentials are accepted on create/patch and NEVER serialized outward
— `_to_out` computes per-scheme configured flags only."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.api.deps import (
    FiltersDep,
    SessionDep,
    apply_filters,
    enforce_static_rules,
    fetch_or_404,
    reject_static_delete,
)
from app.models import A2ATask, RemoteAgent, Skill, Tool, skill_tools
from app.schemas.common import ApiModel
from app.schemas.remote_agent import (
    A2ATaskOut,
    RemoteAgentCreate,
    RemoteAgentOut,
    RemoteAgentPatch,
)

router = APIRouter(prefix="/remote-agents", tags=["remote-agents"])


async def _a2a_on() -> None:
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("a2a_enabled")):
        raise HTTPException(409, "a2a is disabled (a2a_enabled=false)")


async def _tool_counts(session: SessionDep, agent_ids: list[UUID]) -> dict[UUID, int]:
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(Tool.remote_agent_id, func.count())
        .where(Tool.remote_agent_id.in_(agent_ids), Tool.deleted_at.is_(None))
        .group_by(Tool.remote_agent_id)
    )
    return {aid: count for aid, count in rows if aid is not None}


def _to_out(agent: RemoteAgent, tool_count: int = 0) -> RemoteAgentOut:
    out = RemoteAgentOut.model_validate(agent)
    out.tool_count = tool_count
    configured = set((agent.credentials or {}).keys())
    auth: dict[str, Any] = {}
    for name, meta in (agent.auth_schemes or {}).items():
        auth[name] = {**meta, "configured": name in configured}
    out.auth = auth
    if not auth:
        out.auth_status = "open"
    elif any(m["supported"] and m["configured"] for m in auth.values()):
        out.auth_status = "ok"
    elif any(m["supported"] for m in auth.values()):
        out.auth_status = "unconfigured"
    else:
        out.auth_status = "unsupported"
    return out


@router.get("", response_model=list[RemoteAgentOut])
async def list_agents(session: SessionDep, filters: FiltersDep) -> list[RemoteAgentOut]:
    stmt = apply_filters(select(RemoteAgent), RemoteAgent, filters)
    agents = list((await session.execute(stmt)).scalars())
    counts = await _tool_counts(session, [a.id for a in agents])
    return [_to_out(a, counts.get(a.id, 0)) for a in agents]


@router.post("", response_model=RemoteAgentOut, status_code=201)
async def create_agent(body: RemoteAgentCreate, session: SessionDep) -> RemoteAgentOut:
    await _a2a_on()
    from app.a2a.manager import get_manager

    manager = get_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="A2A manager not running")
    # fetch the card up front so the row lands named + validated (spec §19.2)
    try:
        card = await manager.fetch_card(body.card_url)
    except Exception as exc:
        raise HTTPException(422, f"could not fetch agent card: {exc}") from exc
    agent = RemoteAgent(
        name=body.name or card.name,
        description=body.description or card.description or "",
        card_url=body.card_url,
        credentials=body.credentials,
        source="dynamic",
        status="inactive",  # refresh_agent flips to active/error
    )
    session.add(agent)
    await session.commit()
    await manager.refresh_agent(agent.id)
    await session.refresh(agent)
    counts = await _tool_counts(session, [agent.id])
    return _to_out(agent, counts.get(agent.id, 0))


@router.get("/{agent_id}", response_model=RemoteAgentOut)
async def get_agent(agent_id: UUID, session: SessionDep) -> RemoteAgentOut:
    agent = await fetch_or_404(session, RemoteAgent, agent_id)
    counts = await _tool_counts(session, [agent.id])
    return _to_out(agent, counts.get(agent.id, 0))


@router.patch("/{agent_id}", response_model=RemoteAgentOut)
async def patch_agent(
    agent_id: UUID, body: RemoteAgentPatch, session: SessionDep
) -> RemoteAgentOut:
    await _a2a_on()
    agent = await fetch_or_404(session, RemoteAgent, agent_id)
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    enforce_static_rules(agent, set(changes))
    creds = changes.pop("credentials", None)
    if creds is not None:
        merged = dict(agent.credentials or {})
        for scheme, value in creds.items():
            if value is None:
                merged.pop(scheme, None)
            else:
                merged[scheme] = value
        agent.credentials = merged
    for field, value in changes.items():
        setattr(agent, field, value)
    await session.commit()
    await session.refresh(agent)
    counts = await _tool_counts(session, [agent.id])
    return _to_out(agent, counts.get(agent.id, 0))


async def _bound_skills(session: SessionDep, agent_id: UUID) -> list[Skill]:
    stmt = (
        select(Skill)
        .join(skill_tools, skill_tools.c.skill_id == Skill.id)
        .join(Tool, Tool.id == skill_tools.c.tool_id)
        .where(Tool.remote_agent_id == agent_id, Skill.deleted_at.is_(None))
        .distinct()
    )
    return list((await session.execute(stmt)).scalars())


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: UUID, session: SessionDep) -> None:
    await _a2a_on()
    agent = await fetch_or_404(session, RemoteAgent, agent_id)
    reject_static_delete(agent)
    dependents = await _bound_skills(session, agent_id)
    if dependents:
        raise HTTPException(
            status_code=409,
            detail=(
                "agent tools are bound to skills: " + ", ".join(sorted(s.name for s in dependents))
            ),
        )
    now = datetime.now(UTC)
    agent.deleted_at = now
    for tool in (
        await session.execute(select(Tool).where(Tool.remote_agent_id == agent_id))
    ).scalars():
        tool.deleted_at = now
    await session.commit()
    from app.registry_cache import get_cache

    await get_cache().invalidate("tools")


@router.post("/{agent_id}/refresh-card", response_model=RemoteAgentOut)
async def refresh_card(agent_id: UUID, session: SessionDep) -> RemoteAgentOut:
    await _a2a_on()
    agent = await fetch_or_404(session, RemoteAgent, agent_id)
    from app.a2a.manager import get_manager

    manager = get_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="A2A manager not running")
    await manager.refresh_agent(agent.id)
    await session.refresh(agent)
    counts = await _tool_counts(session, [agent.id])
    return _to_out(agent, counts.get(agent.id, 0))


def _task_out(task: A2ATask) -> A2ATaskOut:
    return A2ATaskOut(
        id=str(task.id),
        remote_agent_id=str(task.remote_agent_id),
        run_id=str(task.run_id) if task.run_id else None,
        remote_task_id=task.remote_task_id,
        state=task.state,
        question=task.question,
        error=task.error,
        parked_at=task.parked_at,
        delivered=task.delivered,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("/{agent_id}/tasks", response_model=list[A2ATaskOut])
async def list_tasks(agent_id: UUID, session: SessionDep) -> list[A2ATaskOut]:
    await fetch_or_404(session, RemoteAgent, agent_id)
    rows = (
        await session.execute(
            select(A2ATask)
            .where(A2ATask.remote_agent_id == agent_id)
            .order_by(A2ATask.created_at.desc())
            .limit(50)
        )
    ).scalars()
    return [_task_out(t) for t in rows]


async def _get_agent_task(session: SessionDep, agent_id: UUID, task_id: UUID) -> A2ATask:
    agent = await fetch_or_404(session, RemoteAgent, agent_id)
    task = await session.get(A2ATask, task_id)
    if task is None or task.remote_agent_id != agent.id:
        raise HTTPException(404, "task not found")
    return task


class TaskReply(ApiModel):
    text: str


@router.post("/{agent_id}/tasks/{task_id}/reply", response_model=A2ATaskOut)
async def reply_task(
    agent_id: UUID, task_id: UUID, body: TaskReply, session: SessionDep
) -> A2ATaskOut:
    """Answer an input-required task from the drawer (spec §19.6): terminal
    outcomes deliver through the outbox; still-working tasks re-park so the
    poller resumes watching."""
    await _a2a_on()
    task = await _get_agent_task(session, agent_id, task_id)
    if task.state != "input-required" or not task.remote_task_id:
        raise HTTPException(409, f"task is {task.state} — reply needs input-required")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "reply text is required")
    from app.a2a import client_port
    from app.a2a import tasks as task_store
    from app.a2a.poller import deliver_outcome
    from app.registry_cache import get_cache

    timeout_s = max(int(await get_cache().setting("a2a_task_timeout_s")), 1)
    agent_name = (await session.get(RemoteAgent, agent_id)).name  # type: ignore[union-attr]
    try:
        async with asyncio.timeout(timeout_s):
            outcome = await client_port.send_text(
                agent_id, text, task_id=task.remote_task_id, context_id=task.context_id
            )
    except TimeoutError:
        await task_store.update_task(task_id, parked=True)
        await session.refresh(task)
        return _task_out(task)
    except Exception as exc:
        raise HTTPException(502, f"reply failed: {exc}") from exc
    from app.models import A2A_TERMINAL_STATES

    if outcome.state in A2A_TERMINAL_STATES:
        await deliver_outcome(task_id, agent_id, agent_name, outcome, run_id=task.run_id)
    elif outcome.state == "input-required":
        await task_store.update_task(task_id, state="input-required", question=outcome.question)
    else:
        await task_store.update_task(task_id, parked=True)
    await session.refresh(task)
    return _task_out(task)


@router.post("/{agent_id}/tasks/{task_id}/cancel", response_model=A2ATaskOut)
async def cancel_agent_task(agent_id: UUID, task_id: UUID, session: SessionDep) -> A2ATaskOut:
    await _a2a_on()
    task = await _get_agent_task(session, agent_id, task_id)
    from app.a2a import client_port
    from app.a2a import tasks as task_store

    if task.remote_task_id:
        try:
            await client_port.cancel_task(agent_id, task.remote_task_id)
        except Exception as exc:
            raise HTTPException(502, f"remote cancel failed: {exc}") from exc
    await task_store.update_task(
        task_id, state="canceled", error="cancelled from the task drawer", delivered=True
    )
    await session.refresh(task)
    return _task_out(task)
