"""Idempotent static seed (spec §9).

Upserts match on (source='static', name) — or tool_key for tools — so ids
stay stable across reloads. MCP tool bindings referenced by native skill
documents resolve tolerantly: keys whose tools are not yet ingested (the MCP
manager connects in M2) are re-reconciled on every seed run.
"""

from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.models import McpServer, Skill, SubAgent, Tool
from app.native.provider import native_sub_agents, native_tools, scan_native
from app.skilldoc import scan_skill_files

logger = structlog.get_logger("seed")

SKILLS_DIR = Path(__file__).resolve().parents[1] / "native" / "skills"
AGENTS_DIR = Path(__file__).resolve().parents[1] / "native" / "sub_agents"
# skill names seed_sub_agents composes research-concierge from (spec §9): a
# missing one aborts boot, so app.doclint enforces their presence in the
# shipped skills directory
REQUIRED_STATIC_SKILLS = ("web-research", "file-ops")
# the static sub-agent name seed_sub_agents owns; an .agent.md claiming it
# would fight the code path for the same registry row
STATIC_SEED_AGENT_NAMES = ("research-concierge",)


async def seed_mcp_servers(session: AsyncSession) -> None:
    config = get_config()
    servers: list[dict[str, Any]] = [
        {
            "name": "fetch",
            "description": "Reference MCP server exposing a fetch tool for web pages.",
            "transport": "stdio",
            "command": "uvx",
            # --with mcp<2: published mcp-server-fetch builds break on mcp 2.x
            "args": ["--with", "mcp<2", "mcp-server-fetch"],
        },
        {
            "name": "filesystem",
            "description": "Reference MCP filesystem server sandboxed to the workspace volume.",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", config.workspace_dir],
        },
    ]
    for data in servers:
        existing = (
            await session.execute(
                select(McpServer).where(
                    McpServer.name == data["name"], McpServer.source == "static"
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(McpServer(**data, source="static", status="inactive"))
        else:
            for field, value in data.items():
                if field != "name":
                    setattr(existing, field, value)
    await session.commit()


async def upsert_native_tools(session: AsyncSession) -> None:
    """Registration scan → tools registry (spec §5b). Entries removed from
    code are marked inactive."""
    scan_native()
    registered = native_tools()
    rows = list(
        (
            await session.execute(
                select(Tool).where(Tool.kind == "native", Tool.source == "static")
            )
        ).scalars()
    )
    by_key = {t.tool_key: t for t in rows}
    for name, entry in registered.items():
        tool = by_key.get(name)
        if tool is None:
            session.add(
                Tool(
                    name=name,
                    description=entry.description,
                    kind="native",
                    source="static",
                    tool_name=name,
                    tool_key=name,
                    native_ref=entry.native_ref,
                    input_schema=entry.input_schema,
                )
            )
        else:
            tool.description = entry.description
            tool.native_ref = entry.native_ref
            tool.input_schema = entry.input_schema
            if tool.status == "inactive":
                tool.status = "active"
    for key, tool in by_key.items():
        if key not in registered:
            tool.status = "inactive"
    await session.commit()


async def seed_native_skills(session: AsyncSession) -> None:
    """.skill.md scan → skills registry (spec §3.3). Tool bindings resolve by
    tool_key; not-yet-ingested MCP tools reconcile on later seed runs."""
    for doc in scan_skill_files(SKILLS_DIR):
        skill = (
            await session.execute(
                select(Skill).where(Skill.name == doc.name, Skill.source == "static")
            )
        ).scalar_one_or_none()
        bound_tools = list(
            (
                await session.execute(
                    select(Tool).where(Tool.tool_key.in_(doc.tools), Tool.deleted_at.is_(None))
                )
            ).scalars()
        )
        if skill is None:
            skill = Skill(
                name=doc.name,
                description=doc.description,
                persona=doc.persona,
                instructions=doc.instructions,
                direct_exposure=doc.direct_exposure,
                max_tool_iterations=doc.max_tool_iterations,
                kind="native",
                source="static",
                tools=bound_tools,
            )
            session.add(skill)
        else:
            skill.description = doc.description
            skill.persona = doc.persona
            skill.instructions = doc.instructions
            skill.max_tool_iterations = doc.max_tool_iterations
            skill.tools = bound_tools
        await session.commit()


async def seed_sub_agents(session: AsyncSession) -> None:
    """research-concierge (spec §9): branch + HITL + both skills."""
    skills = {
        s.name: s
        for s in (await session.execute(select(Skill).where(Skill.source == "static"))).scalars()
    }
    research = skills.get("web-research")
    file_ops = skills.get("file-ops")
    if research is None or file_ops is None:
        # reachable from a lint-clean skills directory if someone renames or
        # deletes one of these files — app.doclint guards the shipped dir so
        # the build fails before an image can crash at boot here
        raise RuntimeError(
            "static seed requires the skills " + ", ".join(sorted(REQUIRED_STATIC_SKILLS))
        )
    workflow = {
        "nodes": [
            {"id": "research", "type": "skill", "skill_id": str(research.id)},
            {"id": "approve", "type": "hitl", "prompt": "Save findings to a file?"},
            {"id": "write", "type": "skill", "skill_id": str(file_ops.id)},
        ],
        "edges": [
            {"from": "START", "to": "research"},
            {"from": "research", "to": "approve", "condition": "if research found results"},
            {"from": "research", "to": "END", "condition": "if nothing found"},
            {"from": "approve", "to": "write"},
            {"from": "write", "to": "END"},
        ],
    }
    agent = (
        await session.execute(
            select(SubAgent).where(
                SubAgent.name == "research-concierge", SubAgent.source == "static"
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        agent = SubAgent(
            name="research-concierge",
            description=(
                "Researches a topic on the web and, on approval, saves the findings "
                "to a workspace file."
            ),
            persona="You are a helpful research concierge.",
            workflow=workflow,
            kind="custom",
            source="static",
            direct_exposure=True,
            skills=[research, file_ops],
        )
        session.add(agent)
    else:
        agent.workflow = workflow
        agent.skills = [research, file_ops]
    await session.commit()


async def _resolve_covers(session: AsyncSession, covers: list[str]) -> list[str]:
    """covers_skill_ids entries may be registry uuids or skill NAMES — code
    can't know DB-generated uuids, so native registrations use names and the
    seed pass resolves them here. Unresolvable entries are dropped with a log
    (the skill may ingest on a later seed run and reconcile then)."""
    resolved: list[str] = []
    for ref in covers:
        try:
            UUID(ref)
            resolved.append(ref)
            continue
        except ValueError:
            pass
        skill = (await session.execute(select(Skill).where(Skill.name == ref))).scalar_one_or_none()
        if skill is None:
            logger.warning("native_covers_unresolved", ref=ref)
            continue
        resolved.append(str(skill.id))
    return resolved


async def seed_agent_files(session: AsyncSession, directory: Path | None = None) -> None:
    """.agent.md scan → sub_agents registry (spec §3.4): declarative static
    custom agents. Definition follows the file; user toggles survive reseeds;
    invalid files land as status='error' (never crash boot, never vanish
    silently); removed files mark their rows inactive."""
    from app.agentdoc import scan_agent_files
    from app.factory.dag import validate_workflow
    from app.factory.worker import compile_workflow_check

    docs, parse_errors = scan_agent_files(directory or AGENTS_DIR)
    for err in parse_errors:
        logger.error("agent_file_unparseable", error=err)

    active_skills = {
        s.name: s
        for s in (
            await session.execute(
                select(Skill).where(Skill.status == "active", Skill.deleted_at.is_(None))
            )
        ).scalars()
    }
    seen_refs: set[str] = set()
    for doc in docs:
        ref = f"file:{doc.filename}"
        seen_refs.add(ref)
        errors: list[str] = []
        # by-name sugar → registry uuids; the stored workflow is pure §3.5
        workflow: dict[str, Any] = {"nodes": [], "edges": list(doc.workflow["edges"])}
        for node in doc.workflow["nodes"]:
            node = dict(node)
            skill_name = node.pop("skill", None)
            if skill_name is not None:
                skill = active_skills.get(str(skill_name))
                if skill is None:
                    errors.append(f"skill {skill_name!r} is not an active registry skill")
                else:
                    node["skill_id"] = str(skill.id)
            workflow["nodes"].append(node)
        if not errors:
            active_ids = {str(s.id) for s in active_skills.values()}
            errors = validate_workflow(workflow, active_ids)
        if not errors:
            errors = await compile_workflow_check(
                session,
                workflow,
                persona=doc.persona,
                model=doc.model,
                model_params=doc.model_params,
            )
        if errors:
            logger.error("agent_file_invalid", file=doc.filename, errors=errors)
        by_id = {str(s.id): s for s in active_skills.values()}
        node_skill_ids = {str(nd["skill_id"]) for nd in workflow["nodes"] if nd.get("skill_id")}
        bound = [by_id[sid] for sid in sorted(node_skill_ids) if sid in by_id]
        agent = (
            await session.execute(
                select(SubAgent).where(SubAgent.name == doc.name, SubAgent.source == "static")
            )
        ).scalar_one_or_none()
        if agent is None:
            session.add(
                SubAgent(
                    name=doc.name,
                    description=doc.description,
                    persona=doc.persona,
                    model=doc.model,
                    model_params=doc.model_params,
                    workflow=workflow,
                    kind="custom",
                    source="static",
                    native_ref=ref,
                    direct_exposure=doc.direct_exposure,
                    status="error" if errors else "active",
                    skills=bound,
                )
            )
        else:
            # definition always follows the file; status/direct_exposure are
            # the user's toggles — except error↔active transitions driven by
            # the file's validity itself
            agent.description = doc.description
            agent.persona = doc.persona
            agent.model = doc.model
            agent.model_params = doc.model_params
            agent.workflow = workflow
            agent.native_ref = ref
            agent.skills = bound
            if errors:
                agent.status = "error"
            elif agent.status == "error":
                agent.status = "active"
    # a removed file deactivates its row — history stays intact, no deletes
    file_rows = (
        await session.execute(
            select(SubAgent).where(
                SubAgent.source == "static",
                SubAgent.kind == "custom",
                SubAgent.native_ref.like("file:%"),
            )
        )
    ).scalars()
    for row in file_rows:
        if row.native_ref not in seen_refs and row.status != "inactive":
            logger.warning("agent_file_removed", name=row.name, ref=row.native_ref)
            row.status = "inactive"
    await session.commit()


async def upsert_native_sub_agents(session: AsyncSession) -> None:
    """Native sub agent cards from the @native_sub_agent scan (spec §3.4)."""
    for name, entry in native_sub_agents().items():
        covers = await _resolve_covers(session, entry.covers_skill_ids)
        agent = (
            await session.execute(
                select(SubAgent).where(SubAgent.name == name, SubAgent.source == "static")
            )
        ).scalar_one_or_none()
        if agent is None:
            session.add(
                SubAgent(
                    name=name,
                    description=entry.description,
                    kind="native",
                    source="static",
                    native_ref=entry.native_ref,
                    covers_skill_ids=covers,
                    # spec §3.4: static seeds ship exposed; the toggle stays live
                    direct_exposure=True,
                )
            )
        else:
            agent.description = entry.description
            agent.native_ref = entry.native_ref
            agent.covers_skill_ids = covers
    await session.commit()


# First-boot default-model resolution (spec §13): the code default's provider
# may have no key configured. Preference order — flagships per provider.
_FLAGSHIPS: tuple[tuple[str, str], ...] = (
    ("anthropic", "anthropic:claude-sonnet-4-6"),
    ("google_genai", "google_genai:gemini-3.6-flash"),
    ("openai", "openai:gpt-5.6-luna"),
    ("fake", "fake:scripted"),
)


async def resolve_first_boot_default_model(session: AsyncSession) -> str | None:
    """If no explicit default_model has ever been stored and the code
    default's provider is unconfigured, store the first configured
    provider's flagship. An explicit setting is never touched, and with
    nothing configured at all the code default stands (the UI marks it
    unconfigured until a key arrives)."""
    import structlog

    from app.llm import list_providers
    from app.models import AppSetting
    from app.settings_store import DEFAULTS

    if await session.get(AppSetting, "default_model") is not None:
        return None
    default_provider = str(DEFAULTS["default_model"]).split(":", 1)[0]
    providers = {p.provider_id: p for p in list_providers()}
    default_adapter = providers.get(default_provider)
    if default_adapter is not None and default_adapter.is_configured():
        return None
    for provider_id, ref in _FLAGSHIPS:
        adapter = providers.get(provider_id)
        if adapter is not None and adapter.is_configured():
            session.add(AppSetting(key="default_model", value={"value": ref}))
            await session.commit()
            from app.registry_cache import get_cache

            await get_cache().invalidate("settings")
            structlog.get_logger("seed").info(
                "first_boot_default_model",
                resolved=ref,
                reason=f"{default_provider} unconfigured",
            )
            return ref
    return None


async def seed_all(session: AsyncSession) -> dict[str, int]:
    await seed_mcp_servers(session)
    await upsert_native_tools(session)
    await seed_native_skills(session)
    await seed_sub_agents(session)
    await seed_agent_files(session)
    await upsert_native_sub_agents(session)
    await resolve_first_boot_default_model(session)
    counts: dict[str, int] = {}
    for label, model in {
        "mcp_servers": McpServer,
        "tools": Tool,
        "skills": Skill,
        "sub_agents": SubAgent,
    }.items():
        rows = (await session.execute(select(model).where(model.deleted_at.is_(None)))).scalars()
        counts[label] = len(list(rows))
    return counts
