"""Overlap guard (spec §4): LLM-as-judge duplicate detection at save time.

Before a skill or sub agent is created/updated, the UI calls a check endpoint
that compares the draft against existing registry records — skills against
skills + tools, sub agents against sub agents + skills. The judge runs through
the same provider port as everything else; if it is unavailable the check
fails open (never blocks a save on infrastructure trouble). Tools are exempt
by design: MCP ingest is dynamic and two servers may legitimately expose
similar tools.
"""

from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_session_factory
from app.llm import get_model
from app.models import Skill, SubAgent, Tool
from app.prompts import load_prompt

logger = structlog.get_logger("overlap")

OVERLAP_THRESHOLD_PERCENT = 70


class OverlapVerdict(BaseModel):
    """Judge output: the single strongest overlap found."""

    overlap_percent: int = Field(ge=0, le=100)
    match_type: Literal["skill", "tool", "sub_agent", "none"] = "none"
    match_id: str | None = None
    match_name: str | None = None
    reasoning: str = ""


class OverlapCheckOut(BaseModel):
    overlap: bool
    threshold: int
    overlap_percent: int
    match_type: str
    match_id: str | None
    match_name: str | None
    reasoning: str


def _candidate_line(kind: str, id_: Any, name: str, description: str, extra: str = "") -> str:
    desc = " ".join((description or "").split())[:300]
    return f"- [{kind}] id={id_} name={name!r}: {desc}{extra}"


async def _skill_candidates(exclude_id: UUID | None) -> list[str]:
    async with get_session_factory()() as session:
        skills = list(
            (
                await session.execute(
                    select(Skill).where(Skill.deleted_at.is_(None), Skill.status == "active")
                )
            ).scalars()
        )
        tools = list(
            (
                await session.execute(
                    select(Tool).where(Tool.deleted_at.is_(None), Tool.status == "active")
                )
            ).scalars()
        )
    lines = [
        _candidate_line("skill", s.id, s.name, s.description or s.instructions)
        for s in skills
        if s.id != exclude_id
    ]
    lines += [_candidate_line("tool", t.id, t.tool_key, t.description or "") for t in tools]
    return lines


async def _sub_agent_candidates(exclude_id: UUID | None) -> list[str]:
    async with get_session_factory()() as session:
        agents = list(
            (
                await session.execute(
                    select(SubAgent).where(
                        SubAgent.deleted_at.is_(None), SubAgent.status == "active"
                    )
                )
            ).scalars()
        )
        agent_lines = [
            _candidate_line(
                "sub_agent",
                a.id,
                a.name,
                a.description or "",
                extra=f" (skills: {', '.join(s.name for s in a.skills) or 'code-defined'})",
            )
            for a in agents
            if a.id != exclude_id
        ]
        skills = list(
            (
                await session.execute(
                    select(Skill).where(Skill.deleted_at.is_(None), Skill.status == "active")
                )
            ).scalars()
        )
    agent_lines += [
        _candidate_line("skill", s.id, s.name, s.description or s.instructions) for s in skills
    ]
    return agent_lines


async def _judge(draft_type: str, draft: str, candidates: list[str]) -> OverlapVerdict:
    if not candidates:
        return OverlapVerdict(overlap_percent=0, reasoning="registry has no candidates to compare")
    from app.factory.worker import resolve_node_model

    prompt = (
        load_prompt("overlap_judge")
        .replace("{draft_type}", draft_type)
        .replace("{draft}", draft)
        .replace("{candidates}", "\n".join(candidates))
    )
    try:
        model_ref, params = await resolve_node_model({}, {})
        model = get_model(model_ref, params)
        structured = model.with_structured_output(OverlapVerdict)
        verdict = await structured.ainvoke(prompt)
        assert isinstance(verdict, OverlapVerdict)
        return verdict
    except Exception as exc:  # noqa: BLE001 — the guard must never block saves
        logger.warning("overlap_judge_unavailable", error=str(exc))
        return OverlapVerdict(overlap_percent=0, reasoning=f"judge unavailable: {exc}")


async def check_skill_overlap(
    *,
    name: str,
    description: str,
    instructions: str,
    tool_keys: list[str],
    exclude_id: UUID | None,
) -> OverlapCheckOut:
    draft = (
        f"name: {name!r}\ndescription: {description or '(none)'}\n"
        f"instructions: {' '.join(instructions.split())[:600] or '(none)'}\n"
        f"bound tools: {', '.join(tool_keys) or '(none)'}"
    )
    verdict = await _judge("skill", draft, await _skill_candidates(exclude_id))
    return _to_out(verdict, await _threshold())


async def check_sub_agent_overlap(
    *,
    name: str,
    description: str,
    skill_names: list[str],
    exclude_id: UUID | None,
) -> OverlapCheckOut:
    draft = (
        f"name: {name!r}\ndescription: {description or '(none)'}\n"
        f"workflow skills: {', '.join(skill_names) or '(none)'}"
    )
    verdict = await _judge("sub agent", draft, await _sub_agent_candidates(exclude_id))
    return _to_out(verdict, await _threshold())


async def _threshold() -> int:
    """M40: the gate is the live `overlap_threshold_percent` setting."""
    from app.registry_cache import get_cache

    try:
        return max(0, min(int(await get_cache().setting("overlap_threshold_percent")), 100))
    except Exception:  # noqa: BLE001 — the guard must never block saves
        return OVERLAP_THRESHOLD_PERCENT


def _to_out(verdict: OverlapVerdict, threshold: int = OVERLAP_THRESHOLD_PERCENT) -> OverlapCheckOut:
    return OverlapCheckOut(
        overlap=verdict.overlap_percent >= threshold,
        threshold=threshold,
        overlap_percent=verdict.overlap_percent,
        match_type=verdict.match_type,
        match_id=verdict.match_id,
        match_name=verdict.match_name,
        reasoning=verdict.reasoning,
    )
