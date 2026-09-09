"""Worker factory (spec §6): build_worker(snapshot) -> CompiledStateGraph.

The DAG shell is explicit StateGraph construction — routing, conditional and
error edges, fan-out, joins, and interrupt() are deterministic graph
mechanics. Each `skill` node is a LangChain create_agent tool-loop built at
node-execution time: model via get_model() (resolution order: skill override
→ sub agent override → settings defaults), tools resolved live through the
scoped ToolsRegistryMiddleware, prompt assembled in the §6 order.

State is order-insensitive: node_outputs is a keyed dict merge, so parallel
branches converge to the same state regardless of interleaving. Joins use
LangGraph deferred nodes — they run once every reachable upstream branch has
completed, so branches not taken never deadlock a join (spec §3.5).
"""

import re
from datetime import datetime
from typing import Annotated, Any, TypedDict
from uuid import UUID

import structlog
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_factory
from app.llm import ModelParams, get_model, text_from_content
from app.models import Skill, SubAgent
from app.prompts import load_prompt

logger = structlog.get_logger("factory.worker")


class NodeExecutionError(RuntimeError):
    """A workflow node failed with no error edge — the run fails (spec §3.5)."""

    def __init__(self, node_id: str, error: str) -> None:
        super().__init__(f"node {node_id!r} failed: {error}")
        self.node_id = node_id
        self.error = error


def _merge_node_outputs(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    return {**(a or {}), **(b or {})}


class WorkerState(TypedDict, total=False):
    """Standard worker state schema (spec §6): {messages, task, node_outputs}."""

    messages: Annotated[list[AnyMessage], add_messages]
    task: str
    node_outputs: Annotated[dict[str, Any], _merge_node_outputs]


class ConditionChoice(BaseModel):
    """Router structured output: index of the matching condition."""

    index: int = Field(description="zero-based index of the chosen condition")


def sanitize_tool_name(tool_key: str) -> str:
    """LLM-facing tool name: tool_key with punctuation replaced (provider
    tool-name charsets don't allow dots)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_key)


# ── snapshots (spec §3.6: frozen config at dispatch) ─────────────


async def snapshot_skill(session: AsyncSession, skill: Skill) -> dict[str, Any]:
    return {
        "id": str(skill.id),
        "name": skill.name,
        "persona": skill.persona,
        "instructions": skill.instructions,
        "model": skill.model,
        "model_params": skill.model_params,
        "direct_exposure": skill.direct_exposure,
        "max_tool_iterations": skill.max_tool_iterations,
        "tools": [
            {
                "id": str(t.id),
                "kind": t.kind,
                "tool_name": t.tool_name,
                "tool_key": t.tool_key,
                "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
                "remote_agent_id": str(t.remote_agent_id) if t.remote_agent_id else None,
                "native_ref": t.native_ref,
                "status": t.status,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in skill.tools
            if t.deleted_at is None
        ],
    }


async def snapshot_sub_agent(session: AsyncSession, agent: SubAgent) -> dict[str, Any]:
    workflow = agent.workflow or {}
    skill_ids = [
        UUID(n["skill_id"])
        for n in workflow.get("nodes", [])
        if n.get("type") == "skill" and n.get("skill_id")
    ]
    skills: dict[str, dict[str, Any]] = {}
    if skill_ids:
        rows = (await session.execute(select(Skill).where(Skill.id.in_(skill_ids)))).scalars()
        for s in rows:
            skills[str(s.id)] = await snapshot_skill(session, s)
    return {
        "sub_agent": {
            "id": str(agent.id),
            "name": agent.name,
            "persona": agent.persona,
            "model": agent.model,
            "model_params": agent.model_params,
            "kind": agent.kind,
            "source": agent.source,
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
        },
        "workflow": workflow,
        "skills": skills,
    }


def build_ephemeral_snapshot(
    skill_snapshots: list[dict[str, Any]], task: str = ""
) -> dict[str, Any]:
    """Rung-4 ephemeral dynamic worker (spec §7.1): skills executed as a
    sequential chain; the first skill's persona leads."""
    nodes = [
        {"id": f"step-{i + 1}", "type": "skill", "skill_id": s["id"]}
        for i, s in enumerate(skill_snapshots)
    ]
    edges: list[dict[str, Any]] = [{"from": "START", "to": nodes[0]["id"]}]
    for a, b in zip(nodes, nodes[1:], strict=False):
        edges.append({"from": a["id"], "to": b["id"]})
    edges.append({"from": nodes[-1]["id"], "to": "END"})
    return {
        "sub_agent": {
            "id": None,
            "name": "ephemeral-worker",
            "persona": skill_snapshots[0]["persona"],
            "model": None,
            "model_params": None,
            "kind": "dynamic",
            "source": "dynamic",
            "updated_at": None,
        },
        "workflow": {"nodes": nodes, "edges": edges},
        "skills": {s["id"]: s for s in skill_snapshots},
    }


# ── live tool resolution (used by ToolsRegistryMiddleware) ───────


def _make_mcp_proxy(row: dict[str, Any]) -> BaseTool:
    """Lazy proxy: resolves the live MCP session at call time, so a dead
    server surfaces as a tool error (→ error-edge semantics), and a
    reconnected one works with no rebuild."""
    server_id = row["mcp_server_id"]
    tool_name = row["tool_name"]

    async def call(**kwargs: Any) -> Any:
        from app.mcp.manager import get_manager

        manager = get_manager()
        if manager is None or server_id is None:
            raise RuntimeError(f"MCP manager unavailable for tool {row['tool_key']!r}")
        tools = await manager.get_langchain_tools(UUID(server_id), [tool_name])
        if not tools:
            raise RuntimeError(f"tool {tool_name!r} not exposed by its server")
        return await tools[0].ainvoke(kwargs)

    return StructuredTool(
        name=sanitize_tool_name(row["tool_key"]),
        description=row.get("description") or f"MCP tool {row['tool_key']}",
        args_schema=row.get("input_schema") or {"type": "object", "properties": {}},
        coroutine=call,
    )


def _make_native_tool(row: dict[str, Any]) -> BaseTool | None:
    import inspect

    from app.native.provider import native_tools

    entry = native_tools().get(row["tool_name"])
    if entry is None:
        logger.warning("native_tool_missing", tool_name=row["tool_name"])
        return None
    accepts_config = "config" in inspect.signature(entry.fn).parameters

    async def call(**kwargs: Any) -> Any:
        """Wrap the native callable with usage capture so nested subgraph LLM
        calls report tokens onto the tool_call step (spec §5b guardrail 3)."""
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        from app.orchestrator.middleware import TOOL_USAGE_HOLDER

        usage_handler = UsageMetadataCallbackHandler()
        if accepts_config:
            kwargs["config"] = {"callbacks": [usage_handler]}
        result = await entry.fn(**kwargs)
        holder = TOOL_USAGE_HOLDER.get()
        if holder is not None:
            for usage in usage_handler.usage_metadata.values():
                holder["input_tokens"] = holder.get("input_tokens", 0) + usage.get(
                    "input_tokens", 0
                )
                holder["output_tokens"] = holder.get("output_tokens", 0) + usage.get(
                    "output_tokens", 0
                )
        return result

    return StructuredTool(
        name=sanitize_tool_name(row["tool_key"]),
        description=row.get("description") or entry.description,
        args_schema=entry.input_schema,
        coroutine=call,
    )


def materialize_tool(row: dict[str, Any]) -> BaseTool | None:
    if row["kind"] == "mcp":
        return _make_mcp_proxy(row)
    if row["kind"] == "a2a":
        from app.a2a.proxy import make_a2a_proxy

        return make_a2a_proxy(row, sanitize_tool_name(row["tool_key"]))
    return _make_native_tool(row)


async def resolve_tools_by_ids(tool_ids: list[UUID]) -> list[BaseTool]:
    """Event-invalidated registry read per call (spec §7.0/§7.3): only
    active, non-deleted tools materialize."""
    if not tool_ids:
        return []
    from app.registry_cache import get_cache

    records = await get_cache().tools_by_ids(list(tool_ids))
    out: list[BaseTool] = []
    for record in records:
        tool = materialize_tool(record)
        if tool is not None:
            out.append(tool)
    return out


async def resolve_skill_tools(skill_snapshot: dict[str, Any]) -> list[BaseTool]:
    """Binding = availability, strictly (spec §3.3)."""
    return await resolve_tools_by_ids([UUID(t["id"]) for t in skill_snapshot.get("tools", [])])


# ── prompt assembly (spec §6 order) ──────────────────────────────


def assemble_skill_prompt(
    sub_agent_persona: str,
    skill_persona: str,
    skill_instructions: str,
    node_instructions: str | None = None,
) -> str:
    parts: list[str] = []
    if sub_agent_persona.strip():
        parts.append(sub_agent_persona.strip())
    if skill_persona.strip() and skill_persona.strip() != sub_agent_persona.strip():
        parts.append(skill_persona.strip())
    if skill_instructions.strip():
        parts.append(skill_instructions.strip())
    if node_instructions and node_instructions.strip():
        parts.append(f"Additional instructions for this step:\n{node_instructions.strip()}")
    parts.append(load_prompt("tool_guidance"))
    return "\n\n".join(parts)


# ── model resolution (spec §3.4: skill → sub agent → defaults) ───


async def resolve_node_model(
    skill_snapshot: dict[str, Any], agent_snapshot: dict[str, Any]
) -> tuple[str, ModelParams | None]:
    from app.settings_store import get_setting

    for source in (skill_snapshot, agent_snapshot):
        if source.get("model"):
            raw = source.get("model_params")
            return str(source["model"]), ModelParams.model_validate(raw) if raw else None
    from app.orchestrator.context import ambient_default_override

    override = ambient_default_override()
    if override:
        return override, None
    async with get_session_factory()() as session:
        ref = str(await get_setting(session, "default_model"))
        raw = await get_setting(session, "default_model_params")
    return ref, ModelParams.model_validate(raw) if raw else None


async def _max_tool_iterations(skill_snapshot: dict[str, Any] | None = None) -> int:
    """Per-skill loop budget (spec §3.3): skill override → settings default."""
    if skill_snapshot is not None and skill_snapshot.get("max_tool_iterations"):
        return int(skill_snapshot["max_tool_iterations"])
    from app.registry_cache import get_cache

    return int(await get_cache().setting("max_tool_iterations"))


# ── graph compilation ────────────────────────────────────────────


def _format_prior_outputs(node_outputs: dict[str, Any]) -> str:
    lines: list[str] = []
    for node_id in sorted(node_outputs):
        if node_id.startswith("route:"):
            continue
        out = node_outputs[node_id]
        if out.get("status") == "error":
            lines.append(f"- step {node_id} FAILED: {out.get('error')}")
        elif out.get("status") == "denied":
            lines.append(f"- step {node_id}: denied by human reviewer ({out.get('note', '')})")
        else:
            lines.append(f"- step {node_id}: {out.get('output', '')}")
    if not lines:
        return ""
    return "Outputs of prior workflow steps:\n" + "\n".join(lines)


def _usage_from_messages(messages: list[AnyMessage]) -> dict[str, int]:
    input_tokens = output_tokens = 0
    for m in messages:
        usage = getattr(m, "usage_metadata", None)
        if usage:
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _make_skill_node(
    node: dict[str, Any], agent_snapshot: dict[str, Any], skill_snapshot: dict[str, Any]
) -> Any:
    node_id = node["id"]

    async def run_skill(state: WorkerState, config: RunnableConfig) -> dict[str, Any]:
        from app.orchestrator.middleware import SkillLoopContext, build_middleware_stack

        try:
            model_ref, params = await resolve_node_model(
                skill_snapshot, agent_snapshot["sub_agent"]
            )
            model = get_model(model_ref, params)
            prompt = assemble_skill_prompt(
                agent_snapshot["sub_agent"].get("persona", ""),
                skill_snapshot.get("persona", ""),
                skill_snapshot.get("instructions", ""),
                node.get("instructions"),
            )
            stack = build_middleware_stack(
                SkillLoopContext(
                    skill_id=skill_snapshot["id"],
                    bound_tool_ids=[t["id"] for t in skill_snapshot.get("tools", [])],
                    model=model,
                    max_tool_iterations=await _max_tool_iterations(skill_snapshot),
                )
            )
            agent = create_agent(model, tools=[], system_prompt=prompt, middleware=stack)
            prior = _format_prior_outputs(state.get("node_outputs", {}))
            user_msg = state.get("task", "")
            if prior:
                user_msg = f"{user_msg}\n\n{prior}"
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=user_msg)]}, config=config
            )
            messages = result["messages"]
            final = messages[-1]
            text = text_from_content(final.content)
            return {
                "node_outputs": {
                    node_id: {
                        "status": "ok",
                        "output": text,
                        "skill_id": skill_snapshot["id"],
                        "skill_name": skill_snapshot["name"],
                        "model": model_ref,
                        "effort": params.effort if params else None,
                        "usage": _usage_from_messages(messages),
                    }
                },
                "messages": [AIMessage(content=f"[{node_id}] {text}")],
            }
        except GraphInterrupt:
            # a tool-raised gate inside the skill loop (HITL middleware, a2a
            # input-required — spec §19.5) — NEVER an error edge: let the
            # worker graph pause; resume replays the tool, which adopts its
            # open remote task (§7.1 replay contract)
            raise
        except Exception as exc:  # noqa: BLE001 - error-edge semantics (spec §3.5)
            logger.warning("skill_node_failed", node_id=node_id, error=str(exc))
            return {
                "node_outputs": {
                    node_id: {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "skill_id": skill_snapshot["id"],
                        "skill_name": skill_snapshot.get("name"),
                    }
                }
            }

    return run_skill


def _make_hitl_node(node: dict[str, Any]) -> Any:
    node_id = node["id"]

    async def run_hitl(state: WorkerState) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": node.get("prompt", "Approve?"), "node_id": node_id}
        if node.get("questions"):
            payload["questions"] = node["questions"]  # form gate (spec §3.5)
        decision: dict[str, Any] = interrupt(payload)
        note = str(decision.get("note", ""))
        answers = decision.get("answers") or {}
        if decision.get("decision") == "deny":
            return {
                "node_outputs": {
                    node_id: {"node_type": "hitl", "status": "denied", "note": note or "denied"}
                }
            }
        summary = f"approved{': ' + note if note else ''}"
        if answers:
            answered = "; ".join(f"{k}={v}" for k, v in answers.items())
            summary = f"{summary} — answers: {answered}"
        out: dict[str, Any] = {"node_type": "hitl", "status": "ok", "output": summary}
        if answers:
            out["answers"] = answers
        return {"node_outputs": {node_id: out}}

    return run_hitl


def _make_router_node(
    node: dict[str, Any],
    out_edges: list[dict[str, Any]],
    agent_snapshot: dict[str, Any],
) -> Any:
    node_id = node["id"]
    error_edges = [e for e in out_edges if e.get("on", "success") == "error"]
    success = [e for e in out_edges if e.get("on", "success") == "success"]
    conditional = [e for e in success if e.get("condition")]
    unconditional = [e for e in success if not e.get("condition")]

    async def route(state: WorkerState, config: RunnableConfig) -> dict[str, Any]:
        out = state.get("node_outputs", {}).get(node_id, {})
        record: dict[str, Any] = {"targets": [], "node_id": node_id}
        if out.get("status") == "error":
            if error_edges:
                record["targets"] = [error_edges[0]["to"]]
                record["reason"] = "error edge taken"
            else:
                raise NodeExecutionError(node_id, str(out.get("error")))
        elif out.get("status") == "denied":
            record["targets"] = ["END"]
            record["reason"] = "human denied — routed to END"
        else:
            targets = [e["to"] for e in unconditional]
            if conditional:
                if len(conditional) == 1 and not unconditional:
                    chosen = conditional[0]["to"]
                    record["reason"] = "single conditional edge"
                else:
                    chosen, usage = await _pick_condition(
                        str(out.get("output", "")), conditional, agent_snapshot, config
                    )
                    record["usage"] = usage
                    record["reason"] = "router model selected condition"
                targets.append(chosen)
                record["chosen"] = chosen
            record["targets"] = targets or ["END"]
        return {"node_outputs": {f"route:{node_id}": record}}

    return route


async def _pick_condition(
    output: str,
    conditional_edges: list[dict[str, Any]],
    agent_snapshot: dict[str, Any],
    config: RunnableConfig,
) -> tuple[str, dict[str, int]]:
    model_ref, params = await resolve_node_model({}, agent_snapshot["sub_agent"])
    model = get_model(model_ref, params)
    conditions = "\n".join(f"{i}. {e.get('condition')}" for i, e in enumerate(conditional_edges))
    prompt = load_prompt("router").format(output=output, conditions=conditions)
    structured = model.with_structured_output(ConditionChoice, include_raw=True)
    result: dict[str, Any] = await structured.ainvoke(prompt, config=config)  # type: ignore[assignment]
    choice: ConditionChoice = result["parsed"]
    usage = _usage_from_messages([result["raw"]])
    index = max(0, min(choice.index, len(conditional_edges) - 1))
    return conditional_edges[index]["to"], usage


def build_worker(
    snapshot: dict[str, Any],
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile a workflow DAG snapshot into a LangGraph StateGraph (spec §6)."""
    workflow = snapshot["workflow"]
    nodes: list[dict[str, Any]] = workflow["nodes"]
    edges: list[dict[str, Any]] = workflow["edges"]
    skills: dict[str, dict[str, Any]] = snapshot.get("skills", {})

    graph: StateGraph[WorkerState, Any, Any, Any] = StateGraph(WorkerState)

    incoming: dict[str, int] = {}
    for e in edges:
        if e["to"] not in {"END"}:
            incoming[e["to"]] = incoming.get(e["to"], 0) + 1

    for node in nodes:
        node_id = node["id"]
        if node["type"] == "skill":
            skill_snapshot = skills.get(node.get("skill_id", ""))
            if skill_snapshot is None:
                raise ValueError(
                    f"node {node_id!r}: skill {node.get('skill_id')!r} not in snapshot"
                )
            fn = _make_skill_node(node, snapshot, skill_snapshot)
        else:
            fn = _make_hitl_node(node)
        # deferred join: runs once all reachable upstream branches complete
        graph.add_node(node_id, fn, defer=incoming.get(node_id, 0) > 1)

    out_edges: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        out_edges.setdefault(e["from"], []).append(e)

    for start_edge in out_edges.get("START", []):
        graph.add_edge(START, start_edge["to"])

    for node in nodes:
        node_id = node["id"]
        node_edges = out_edges.get(node_id, [])
        # every node routes through a router step: it enforces no-error-edge
        # failure semantics (spec §3.5) and records the decision in state
        router_id = f"__route__{node_id}"
        graph.add_node(router_id, _make_router_node(node, node_edges, snapshot))
        graph.add_edge(node_id, router_id)
        possible = sorted({e["to"] for e in node_edges} | {"END"})
        path_map = {t: (END if t == "END" else t) for t in possible}

        def _select(state: WorkerState, *, _key: str = f"route:{node_id}") -> list[str]:
            targets: list[str] = state.get("node_outputs", {}).get(_key, {}).get("targets", [])
            return targets or ["END"]

        graph.add_conditional_edges(router_id, _select, path_map)

    return graph.compile(checkpointer=checkpointer)


async def compile_workflow_check(
    session: AsyncSession,
    workflow: dict[str, Any],
    persona: str = "",
    model: str | None = None,
    model_params: dict[str, Any] | None = None,
) -> list[str]:
    """Compile-time = save-time (spec §6): run a full factory compile without
    invocation; returns compile errors (empty = compiles)."""
    skill_ids = [
        UUID(n["skill_id"])
        for n in workflow.get("nodes", [])
        if n.get("type") == "skill" and n.get("skill_id")
    ]
    skills: dict[str, dict[str, Any]] = {}
    if skill_ids:
        rows = (await session.execute(select(Skill).where(Skill.id.in_(skill_ids)))).scalars()
        for s in rows:
            skills[str(s.id)] = await snapshot_skill(session, s)
    snapshot = {
        "sub_agent": {
            "id": None,
            "name": "compile-check",
            "persona": persona,
            "model": model,
            "model_params": model_params,
            "kind": "custom",
            "source": "dynamic",
            "updated_at": None,
        },
        "workflow": workflow,
        "skills": skills,
    }
    try:
        build_worker(snapshot, checkpointer=None)
    except Exception as exc:  # noqa: BLE001 - reported as save-time validation
        return [f"workflow does not compile: {exc}"]
    return []


def get_native_worker(name: str, checkpointer: BaseCheckpointSaver[Any] | None) -> Any:
    """Native sub agent invocation (spec §3.4): the worker factory is bypassed —
    the registered build callable returns the compiled graph directly."""
    from app.native.provider import native_sub_agents

    entry = native_sub_agents().get(name)
    if entry is None:
        raise KeyError(f"native sub agent {name!r} is not registered")
    return entry.build(checkpointer)


# ── compile cache (spec §6): keyed by (sub_agent_id, updated_at) ─

_WORKER_CACHE: dict[tuple[str, str, int], CompiledStateGraph[Any, Any, Any, Any]] = {}


def get_compiled_worker(
    snapshot: dict[str, Any],
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    agent = snapshot["sub_agent"]
    if agent.get("id") is None:  # ephemeral workers are never cached
        return build_worker(snapshot, checkpointer)
    key = (str(agent["id"]), str(agent.get("updated_at")), id(checkpointer))
    cached = _WORKER_CACHE.get(key)
    if cached is None:
        cached = build_worker(snapshot, checkpointer)
        _WORKER_CACHE[key] = cached
    return cached


def compiled_worker_cache_info() -> dict[str, int]:
    return {"size": len(_WORKER_CACHE)}


def clear_worker_cache() -> None:
    _WORKER_CACHE.clear()


def utcnow_iso() -> str:  # pragma: no cover - convenience
    return datetime.utcnow().isoformat()
