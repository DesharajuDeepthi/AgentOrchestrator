"""Shared helpers for factory/orchestrator tests: build registry rows directly.

Direct-row writes invalidate the registry cache afterward, matching the
contract every real write path honors (spec §7.3)."""

from typing import Any
from uuid import UUID, uuid4

from app.db import get_session_factory
from app.models import Skill, SubAgent, Tool


async def create_tool(**kw: Any) -> Tool:
    defaults: dict[str, Any] = {
        "name": kw.get("tool_name", f"tool-{uuid4().hex[:6]}"),
        "kind": "native",
        "tool_name": kw.get("tool_name", f"tool-{uuid4().hex[:6]}"),
        "tool_key": kw.get("tool_key", f"key-{uuid4().hex[:6]}"),
        "source": "dynamic",
        "input_schema": {"type": "object", "properties": {}},
    }
    defaults.update(kw)
    async with get_session_factory()() as session:
        tool = Tool(**defaults)
        session.add(tool)
        await session.commit()
        await session.refresh(tool)
    from app.registry_cache import get_cache

    await get_cache().invalidate("tools")
    return tool


async def create_skill(
    name: str | None = None, tools: list[Tool] | None = None, **kw: Any
) -> Skill:
    defaults: dict[str, Any] = {
        "name": name or f"skill-{uuid4().hex[:6]}",
        "description": "test skill",
        "persona": "You are a test skill persona.",
        "instructions": "Do the task.",
        "kind": "custom",
        "source": "dynamic",
    }
    defaults.update(kw)
    async with get_session_factory()() as session:
        skill = Skill(**defaults)
        if tools:
            skill.tools = [await session.merge(t) for t in tools]
        session.add(skill)
        await session.commit()
        await session.refresh(skill)
    from app.registry_cache import get_cache

    await get_cache().invalidate("skills")
    return skill


async def create_sub_agent(
    workflow: dict[str, Any], name: str | None = None, **kw: Any
) -> SubAgent:
    defaults: dict[str, Any] = {
        "name": name or f"agent-{uuid4().hex[:6]}",
        "description": "test agent",
        "persona": "You are the sub agent persona.",
        "kind": "custom",
        "source": "dynamic",
        "workflow": workflow,
    }
    defaults.update(kw)
    async with get_session_factory()() as session:
        agent = SubAgent(**defaults)
        # derive sub_agent_skills from the workflow (as the API does at save)
        skill_ids = {
            UUID(n["skill_id"])
            for n in workflow.get("nodes", [])
            if n.get("type") == "skill" and n.get("skill_id")
        }
        if skill_ids:
            from sqlalchemy import select

            agent.skills = list(
                (await session.execute(select(Skill).where(Skill.id.in_(skill_ids)))).scalars()
            )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    from app.registry_cache import get_cache

    await get_cache().invalidate("sub_agents")
    return agent


async def load_snapshot(agent_id: UUID) -> dict[str, Any]:
    from app.factory.worker import snapshot_sub_agent

    async with get_session_factory()() as session:
        agent = await session.get(SubAgent, agent_id)
        assert agent is not None
        return await snapshot_sub_agent(session, agent)
