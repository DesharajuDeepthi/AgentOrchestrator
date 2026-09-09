"""Skills registry API (spec §4, §3.3)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import (
    FiltersDep,
    SessionDep,
    apply_filters,
    enforce_static_rules,
    fetch_or_404,
    reject_static_delete,
)
from app.llm import ModelParams, validate_model_selection
from app.models import Skill, SubAgent, Tool, sub_agent_skills
from app.overlap import OverlapCheckOut, check_skill_overlap
from app.registry_cache import get_cache
from app.retrieval import schedule_embedding
from app.schemas.common import ApiModel
from app.schemas.skill import SkillCreate, SkillOut, SkillOverlapCheck, SkillPatch
from app.schemas.sub_agent import SubAgentOut
from app.skilldoc import validate_mentions

router = APIRouter(prefix="/skills", tags=["skills"])


async def _resolve_tools(session: SessionDep, tool_ids: list[UUID]) -> list[Tool]:
    """Strict binding validation: all ids must exist, be active, not deleted."""
    if not tool_ids:
        return []
    tools = list((await session.execute(select(Tool).where(Tool.id.in_(tool_ids)))).scalars())
    found = {t.id for t in tools}
    errors = [f"unknown tool id {tid}" for tid in tool_ids if tid not in found]
    errors += [
        f"tool {t.tool_key!r} is not active (status={t.status})"
        for t in tools
        if t.status != "active" or t.deleted_at is not None
    ]
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    return tools


def _validate_model_fields(model: str | None, model_params: dict[str, Any] | None) -> None:
    if model_params is not None and model is None:
        raise HTTPException(status_code=422, detail="model_params requires model to be set")
    if model is not None:
        try:
            params = ModelParams.model_validate(model_params) if model_params else None
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid model_params: {exc}") from exc
        errors = validate_model_selection(model, params)
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))


def _validate_instruction_mentions(instructions: str, tools: list[Tool]) -> None:
    errors = validate_mentions(instructions, [t.tool_key for t in tools])
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))


@router.get("", response_model=list[SkillOut])
async def list_skills(session: SessionDep, filters: FiltersDep) -> list[Skill]:
    stmt = apply_filters(select(Skill), Skill, filters)
    return list((await session.execute(stmt)).scalars())


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(body: SkillCreate, session: SessionDep) -> Skill:
    tools = await _resolve_tools(session, body.tool_ids)
    _validate_model_fields(body.model, body.model_params)
    _validate_instruction_mentions(body.instructions, tools)
    skill = Skill(
        name=body.name,
        description=body.description,
        persona=body.persona,
        instructions=body.instructions,
        direct_exposure=body.direct_exposure,
        model=body.model,
        model_params=body.model_params,
        max_tool_iterations=body.max_tool_iterations,
        kind="custom",
        source="dynamic",
        tools=tools,
    )
    session.add(skill)
    await session.commit()
    await session.refresh(skill)
    await get_cache().invalidate("skills")
    schedule_embedding("skills", str(skill.id))
    return skill


class OverlapAck(ApiModel):
    """M44: content-free record that a human saved past the §4 overlap
    warning — the capture a future threshold-tuner would need."""

    draft_type: str  # 'skill' | 'sub_agent'
    overlap_percent: int


@router.post("/overlap-ack", status_code=204)
async def overlap_ack(body: OverlapAck) -> None:
    import structlog

    from app import obs
    from app.registry_cache import get_cache

    if body.draft_type not in ("skill", "sub_agent"):
        raise HTTPException(422, "draft_type must be 'skill' or 'sub_agent'")
    threshold = int(await get_cache().setting("overlap_threshold_percent") or 70)
    structlog.get_logger("registry").info(
        "overlap_override",
        tier="registry",
        kind=body.draft_type,
        overlap_percent=max(0, min(100, body.overlap_percent)),
        threshold=threshold,
    )
    obs.OVERLAP_OVERRIDES.labels(draft_type=body.draft_type).inc()


@router.post("/check-overlap", response_model=OverlapCheckOut)
async def skill_overlap(body: SkillOverlapCheck, session: SessionDep) -> OverlapCheckOut:
    """Pre-save LLM-as-judge duplicate check (spec §4). Advisory: the UI asks
    the user to confirm or cancel; this endpoint never blocks anything."""
    tools = list((await session.execute(select(Tool).where(Tool.id.in_(body.tool_ids)))).scalars())
    return await check_skill_overlap(
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        tool_keys=[t.tool_key for t in tools],
        exclude_id=body.exclude_id,
    )


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(skill_id: UUID, session: SessionDep) -> Skill:
    return await fetch_or_404(session, Skill, skill_id)


@router.patch("/{skill_id}", response_model=SkillOut)
async def patch_skill(skill_id: UUID, body: SkillPatch, session: SessionDep) -> Skill:
    skill = await fetch_or_404(session, Skill, skill_id)
    changes = body.model_dump(exclude_unset=True)
    enforce_static_rules(skill, set(changes))

    new_tools = skill.tools
    if "tool_ids" in changes:
        new_tools = await _resolve_tools(session, body.tool_ids or [])
    if "model" in changes or "model_params" in changes:
        model = changes.get("model", skill.model)
        model_params = changes.get("model_params", skill.model_params)
        _validate_model_fields(model, model_params)
    if "instructions" in changes or "tool_ids" in changes:
        instructions = changes.get("instructions", skill.instructions)
        _validate_instruction_mentions(instructions, new_tools)

    for f, v in changes.items():
        if f == "tool_ids":
            skill.tools = new_tools
        else:
            setattr(skill, f, v)
    await session.commit()
    await session.refresh(skill)
    await get_cache().invalidate("skills")
    schedule_embedding("skills", str(skill.id))
    return skill


async def _dependent_sub_agents(session: SessionDep, skill_id: UUID) -> list[SubAgent]:
    stmt = (
        select(SubAgent)
        .join(sub_agent_skills, sub_agent_skills.c.sub_agent_id == SubAgent.id)
        .where(
            sub_agent_skills.c.skill_id == skill_id,
            SubAgent.deleted_at.is_(None),
            SubAgent.status == "active",
        )
        .order_by(SubAgent.name)
    )
    return list((await session.execute(stmt)).scalars())


@router.get("/{skill_id}/sub-agents", response_model=list[SubAgentOut])
async def skill_sub_agents(skill_id: UUID, session: SessionDep) -> list[SubAgent]:
    await fetch_or_404(session, Skill, skill_id)
    stmt = (
        select(SubAgent)
        .join(sub_agent_skills, sub_agent_skills.c.sub_agent_id == SubAgent.id)
        .where(sub_agent_skills.c.skill_id == skill_id, SubAgent.deleted_at.is_(None))
        .order_by(SubAgent.name)
    )
    return list((await session.execute(stmt)).scalars())


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: UUID, session: SessionDep) -> None:
    skill = await fetch_or_404(session, Skill, skill_id)
    reject_static_delete(skill)
    dependents = await _dependent_sub_agents(session, skill_id)
    if dependents:
        raise HTTPException(
            status_code=409,
            detail=(
                "skill is referenced by active sub agents: "
                + ", ".join(sorted(a.name for a in dependents))
            ),
        )
    skill.deleted_at = datetime.now(UTC)
    await session.commit()
    await get_cache().invalidate("skills")
