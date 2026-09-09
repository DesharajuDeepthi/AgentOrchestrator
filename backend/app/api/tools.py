"""Tools registry API (spec §4). Tools are created by MCP ingestion or the
native scan — never via API."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import (
    FiltersDep,
    SessionDep,
    apply_filters,
    enforce_static_rules,
    fetch_or_404,
)
from app.models import Skill, Tool, skill_tools
from app.registry_cache import get_cache
from app.retrieval import schedule_embedding
from app.schemas.skill import SkillOut
from app.schemas.tool import ToolOut, ToolPatch

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolOut])
async def list_tools(session: SessionDep, filters: FiltersDep) -> list[Tool]:
    stmt = apply_filters(select(Tool), Tool, filters)
    return list((await session.execute(stmt)).scalars())


@router.get("/{tool_id}", response_model=ToolOut)
async def get_tool(tool_id: UUID, session: SessionDep) -> Tool:
    return await fetch_or_404(session, Tool, tool_id)


@router.patch("/{tool_id}", response_model=ToolOut)
async def patch_tool(tool_id: UUID, body: ToolPatch, session: SessionDep) -> Tool:
    tool = await fetch_or_404(session, Tool, tool_id)
    changes = body.model_dump(exclude_unset=True)
    enforce_static_rules(tool, set(changes))
    if "tool_key" in changes and changes["tool_key"] != tool.tool_key:
        collision = (
            await session.execute(
                select(Tool).where(Tool.tool_key == changes["tool_key"], Tool.id != tool.id)
            )
        ).scalar_one_or_none()
        if collision is not None:
            raise HTTPException(
                status_code=409, detail=f"tool_key {changes['tool_key']!r} is already in use"
            )
    for f, v in changes.items():
        setattr(tool, f, v)
    await session.commit()
    await session.refresh(tool)
    await get_cache().invalidate("tools")
    schedule_embedding("tools", str(tool.id))
    return tool


async def _skills_of_tool(session: SessionDep, tool_id: UUID) -> list[Skill]:
    stmt = (
        select(Skill)
        .join(skill_tools, skill_tools.c.skill_id == Skill.id)
        .where(skill_tools.c.tool_id == tool_id, Skill.deleted_at.is_(None))
        .order_by(Skill.name)
    )
    return list((await session.execute(stmt)).scalars())


@router.get("/{tool_id}/skills", response_model=list[SkillOut])
async def tool_skills(tool_id: UUID, session: SessionDep) -> list[Skill]:
    await fetch_or_404(session, Tool, tool_id)
    return await _skills_of_tool(session, tool_id)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(tool_id: UUID, session: SessionDep) -> None:
    tool = await fetch_or_404(session, Tool, tool_id)
    if tool.source == "static":
        raise HTTPException(
            status_code=403,
            detail="static records cannot be deleted; toggle status to 'inactive' instead",
        )
    dependents = await _skills_of_tool(session, tool_id)
    if dependents:
        raise HTTPException(
            status_code=409,
            detail="tool is bound to skills: " + ", ".join(sorted(s.name for s in dependents)),
        )
    tool.deleted_at = datetime.now(UTC)
    await session.commit()
    await get_cache().invalidate("tools")
