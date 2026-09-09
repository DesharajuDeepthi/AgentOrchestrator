"""Graph-mode orchestrator (spec §7.1): a hand-built StateGraph —
plan → resolve → dispatch (parallel) → aggregate. Never create_agent at the
shell; rung-1 loops and the fallback loop use the §7.0 stacks."""

from typing import Annotated, Any, TypedDict
from uuid import UUID

import structlog
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from app.db import get_session_factory
from app.factory.worker import resolve_node_model
from app.llm import get_model, text_from_content, thinking_from_content
from app.models import Run
from app.orchestrator.context import require_run_context
from app.orchestrator.ladder import (
    Resolution,
    ResolutionError,
    execute_resolution,
    resolve_capability,
)
from app.orchestrator.planner import PlanFailure, run_planner
from app.prompts import load_prompt
from app.settings_store import get_setting, get_settings

logger = structlog.get_logger("orchestrator.graph")


class RunFailed(RuntimeError):
    """The run fails with a clear chat message (spec §7.1)."""


def _merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    return {**(a or {}), **(b or {})}


class OrchestratorState(TypedDict, total=False):
    task: str
    history: str
    entries: list[dict[str, Any]]
    resolutions: Annotated[dict[str, Any], _merge]
    outputs: Annotated[dict[str, Any], _merge]
    direct_answer: str | None
    use_fallback: bool
    max_parallel: int
    answer: str


async def _planner_model() -> Any:
    from app.orchestrator.context import ambient_default_override

    async with get_session_factory()() as session:
        ref = (
            await get_setting(session, "planner_model")
            or ambient_default_override()
            or await get_setting(session, "default_model")
        )
        raw = await get_setting(session, "planner_model_params") or await get_setting(
            session, "default_model_params"
        )
    from app.llm import ModelParams

    return str(ref), get_model(str(ref), ModelParams.model_validate(raw) if raw else None)


async def _aggregator_model() -> tuple[str, Any]:
    from app.orchestrator.context import ambient_default_override

    async with get_session_factory()() as session:
        ref = (
            await get_setting(session, "aggregator_model")
            or ambient_default_override()
            or await get_setting(session, "default_model")
        )
        raw = await get_setting(session, "aggregator_model_params") or await get_setting(
            session, "default_model_params"
        )
    from app.llm import ModelParams

    return str(ref), get_model(str(ref), ModelParams.model_validate(raw) if raw else None)


async def plan_node(state: OrchestratorState) -> dict[str, Any]:
    ctx = require_run_context()
    async with get_session_factory()() as session:
        max_steps = int(await get_setting(session, "max_plan_steps"))
        fallback_enabled = bool(await get_setting(session, "orchestrator_full_fallback_enabled"))
    ref, model = await _planner_model()
    step_id = await ctx.recorder.start_step("plan", tier="orchestrator", model=ref)
    try:
        async with get_session_factory()() as session:
            plan, raw_outputs, usage = await run_planner(
                session, model, state["task"], state.get("history", ""), max_steps
            )
    except PlanFailure as exc:
        await ctx.recorder.finish_step(step_id, status="failed", error="; ".join(exc.errors))
        async with get_session_factory()() as session:
            run = await session.get(Run, ctx.run_id)
            if run is not None:
                run.plan = {"errors": exc.errors, "raw_planner_outputs": exc.raw_outputs}
                await session.commit()
        raise RunFailed(
            "the planner could not produce a valid plan after one repair attempt: "
            + "; ".join(exc.errors)
        ) from exc
    entries = [e.model_dump() for e in plan.entries]
    plan_payload = {
        "entries": entries,
        "direct_answer": plan.direct_answer,
        "no_confident_match": plan.no_confident_match,
    }
    async with get_session_factory()() as session:
        run = await session.get(Run, ctx.run_id)
        if run is not None:
            run.plan = plan_payload
            await session.commit()
    await ctx.recorder.finish_step(
        step_id,
        output=plan_payload,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
    )
    ctx.recorder.emit("plan", {"entries": entries, "mode": "graph"})

    use_fallback = False
    if plan.no_confident_match or (not entries and plan.direct_answer is None):
        if not fallback_enabled:
            raise RunFailed(
                "no capability confidently matches this request and the "
                "full-catalog fallback is disabled"
            )
        use_fallback = True
    return {
        "entries": entries,
        "direct_answer": plan.direct_answer,
        "use_fallback": use_fallback,
    }


async def resolve_node(state: OrchestratorState) -> dict[str, Any]:
    """Pure-code ladder resolution; every decision logged as a route step."""
    ctx = require_run_context()
    resolutions: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for entry in state.get("entries", []):
        try:
            resolution = await resolve_capability(entry["capability"])
            await ctx.recorder.record_route(
                capability=entry["capability"],
                rung=resolution.rung,
                resolved_to=resolution.as_route(),
                kind=resolution.kind,
                source=resolution.source,
            )
            resolutions[entry["id"]] = resolution.__dict__
        except ResolutionError as exc:
            failures[entry["id"]] = str(exc)
    if failures and len(failures) == len(state.get("entries", [])):
        # every plan entry failed resolution → fallback (spec §7.0)
        async with get_session_factory()() as session:
            fallback_enabled = bool(
                await get_setting(session, "orchestrator_full_fallback_enabled")
            )
        if fallback_enabled:
            return {"use_fallback": True, "resolutions": {}}
        raise RunFailed(
            "no plan entry could be resolved to a capability: "
            + "; ".join(f"{k}: {v}" for k, v in failures.items())
        )
    outputs = {
        entry_id: {"status": "error", "error": message} for entry_id, message in failures.items()
    }
    result: dict[str, Any] = {"resolutions": resolutions}
    if outputs:
        result["outputs"] = outputs
    # snapshot frozen at dispatch (spec §3.6)
    async with get_session_factory()() as session:
        run = await session.get(Run, ctx.run_id)
        if run is not None:
            run.snapshot = {
                entry_id: {k: v for k, v in res.items() if k != "payload"}
                | {"payload_kinds": sorted((res.get("payload") or {}).keys())}
                | {"payload": res.get("payload")}
                for entry_id, res in resolutions.items()
            }
            await session.commit()
    return result


def after_plan(state: OrchestratorState) -> str:
    # a direct_answer only short-circuits when the planner produced NO
    # entries — for multi-part requests it may answer the trivial part
    # directly while still planning capability entries for the rest
    if state.get("direct_answer") is not None and not state.get("entries"):
        return "aggregate"
    if state.get("use_fallback"):
        return "fallback"
    return "resolve"


def after_resolve(state: OrchestratorState) -> str:
    if state.get("use_fallback"):
        return "fallback"
    return "coordinate"


async def coordinate_node(state: OrchestratorState) -> dict[str, Any]:
    async with get_session_factory()() as session:
        max_parallel = int(await get_setting(session, "max_parallel_dispatch"))
    return {"max_parallel": max_parallel}


def dispatch_ready(state: OrchestratorState) -> list[Send] | str:
    """Entries with no unmet depends_on dispatch in parallel (spec §7.1)."""
    outputs = state.get("outputs", {})
    resolutions = state.get("resolutions", {})
    ready: list[dict[str, Any]] = []
    pending = 0
    for entry in state.get("entries", []):
        if entry["id"] in outputs or entry["id"] not in resolutions:
            continue
        pending += 1
        if all(dep in outputs for dep in entry.get("depends_on", [])):
            ready.append(entry)
    if not pending:
        return "aggregate"
    if not ready:
        # dependencies reference failed/unresolved entries — fail them now
        return "aggregate"
    max_parallel = int(state.get("max_parallel", 4))
    return [
        Send("dispatch", {"entry": entry, "resolution": resolutions.get(entry["id"])})
        for entry in ready[:max_parallel]
    ]


async def dispatch_node(payload: Any) -> dict[str, Any]:
    entry = payload["entry"]
    # rebuild the Resolution from checkpoint-safe state on replay
    state_res = payload.get("resolution")
    if state_res is None:
        resolution = await resolve_capability(entry["capability"])
    else:
        resolution = Resolution(**state_res)
    task = entry["task"]
    result = await execute_resolution(resolution, task, entry["id"])
    if result.get("status") == "error" and result.get("fatal"):
        raise RunFailed(str(result.get("error")))
    return {"outputs": {entry["id"]: result}}


async def fallback_node(state: OrchestratorState) -> dict[str, Any]:
    """Self-service full-catalog fallback (spec §7.0)."""
    from langchain.agents import create_agent

    from app.factory.worker import _usage_from_messages
    from app.orchestrator.middleware import FallbackLoopContext, build_middleware_stack

    ctx = require_run_context()
    ctx.flags.full_catalog = True
    await ctx.recorder.record_route(
        capability={"type": "full_catalog"},
        rung="fallback",
        resolved_to={"entity_id": None, "entity_name": "full-catalog fallback"},
        kind="orchestrator",
    )
    step_id = await ctx.recorder.start_step(
        "skill",
        tier="orchestrator",
        kind="fallback",
        entity_name="full-catalog fallback",
        input={"task": state["task"]},
        emit_dispatch=True,
    )
    from app.orchestrator.middleware import current_step

    try:
        model_ref, params = await resolve_node_model({}, {})
        model = get_model(model_ref, params)
        async with get_session_factory()() as session:
            max_iter = int(await get_setting(session, "max_tool_iterations"))
        stack = build_middleware_stack(
            FallbackLoopContext(model=model, max_tool_iterations=max_iter)
        )
        agent = create_agent(
            model,
            tools=[],
            system_prompt=(
                "You are the concierge orchestrator in full-catalog fallback mode: "
                "routing by description failed, so every active tool and skill is "
                "available to you directly. Solve the user's request yourself."
            ),
            middleware=stack,
        )
        with current_step(step_id):
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=state["task"])]},
                config={"callbacks": ctx.callbacks},
            )
        messages = result["messages"]
        text = text_from_content(messages[-1].content)
        usage = _usage_from_messages(messages)
        await ctx.recorder.finish_step(
            step_id,
            output={"output": text},
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            emit_dispatch=True,
        )
        return {"outputs": {"fallback": {"status": "ok", "output": text}}}
    except Exception as exc:  # noqa: BLE001
        await ctx.recorder.finish_step(step_id, status="failed", error=str(exc), emit_dispatch=True)
        raise RunFailed(f"full-catalog fallback failed: {exc}") from exc


async def aggregator_memory_block(task: str) -> str:
    """The §16.3 remembered-context block for the aggregator surface
    (§18.2). Fail-open; empty string when memory is dark."""
    from app.memory.inject import build_memory_block
    from app.orchestrator.context import get_run_context

    ctx = get_run_context()
    block, _stats = await build_memory_block(
        task,
        conversation_id=ctx.conversation_id if ctx else None,
        surface="aggregator",
    )
    return block + "\n" if block else ""


async def aggregate_node(state: OrchestratorState) -> dict[str, Any]:
    """Final merge; aggregator tokens stream as SSE `token` events."""
    ctx = require_run_context()
    if state.get("direct_answer") is not None and not state.get("entries"):
        answer = str(state["direct_answer"])
        ctx.recorder.emit("token", {"text": answer})
        return {"answer": answer}
    ref, model = await _aggregator_model()
    step_id = await ctx.recorder.start_step("aggregate", tier="orchestrator", model=ref)
    outputs = dict(state.get("outputs", {}))
    if state.get("direct_answer"):
        # the planner answered part of the request itself — merge it with
        # the dispatched outputs instead of discarding either
        outputs["planner-direct"] = {"status": "ok", "output": str(state["direct_answer"])}
    formatted = "\n\n".join(
        f"[{entry_id}] status={o.get('status')}\n" + str(o.get("output") or o.get("error") or "")
        for entry_id, o in sorted(outputs.items())
    )
    # §18.2: the aggregator gains the same fenced remembered-context block
    # the planner has — budgeted, fail-open, "" when memory is dark
    memory_block = await aggregator_memory_block(state["task"])
    prompt = memory_block + load_prompt("aggregator").format(task=state["task"], outputs=formatted)
    usage = {"input_tokens": 0, "output_tokens": 0}

    async def stream_answer() -> str:
        chunks: list[str] = []
        async for chunk in model.astream(prompt, config={"callbacks": ctx.callbacks}):
            thinking = thinking_from_content(chunk.content)
            if thinking:
                ctx.recorder.emit("thinking", {"text": thinking})
            text = text_from_content(chunk.content)
            if text:
                chunks.append(text)
                ctx.recorder.emit("token", {"text": text})
            meta = getattr(chunk, "usage_metadata", None)
            if meta:
                usage["input_tokens"] += meta.get("input_tokens", 0)
                usage["output_tokens"] += meta.get("output_tokens", 0)
        return "".join(chunks)

    # thinking models occasionally return an empty completion — retry once
    # before failing the run (same repair-once policy as the planner)
    answer = await stream_answer()
    if not answer.strip():
        logger.warning("aggregator_empty_answer_retry", run_id=str(ctx.run_id), model=ref)
        answer = await stream_answer()
    if not answer.strip():
        await ctx.recorder.finish_step(
            step_id,
            status="failed",
            error="aggregator returned an empty answer twice",
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        raise RunFailed("aggregation produced an empty answer after a retry")
    await ctx.recorder.finish_step(
        step_id,
        output={"answer": answer},
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
    )
    return {"answer": answer}


def build_orchestrator_graph(
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph: StateGraph[OrchestratorState, Any, Any, Any] = StateGraph(OrchestratorState)
    graph.add_node("plan", plan_node)
    graph.add_node("resolve", resolve_node)
    graph.add_node("coordinate", coordinate_node, defer=True)
    graph.add_node("dispatch", dispatch_node)  # type: ignore[arg-type]
    graph.add_node("fallback", fallback_node)
    graph.add_node("aggregate", aggregate_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", after_plan, {"aggregate": "aggregate", "fallback": "fallback", "resolve": "resolve"}
    )
    graph.add_conditional_edges(
        "resolve", after_resolve, {"fallback": "fallback", "coordinate": "coordinate"}
    )
    graph.add_conditional_edges(
        "coordinate", dispatch_ready, {"aggregate": "aggregate", "dispatch": "dispatch"}
    )
    graph.add_edge("dispatch", "coordinate")
    graph.add_edge("fallback", "aggregate")
    graph.add_edge("aggregate", END)
    return graph.compile(checkpointer=checkpointer)


async def build_history(conversation_id: UUID) -> str:
    """Full conversation history for the planner (spec §3.6), capped."""
    from sqlalchemy import select

    async with get_session_factory()() as session:
        runs = list(
            (
                await session.execute(
                    select(Run)
                    .where(Run.conversation_id == conversation_id, Run.status == "completed")
                    .order_by(Run.started_at)
                )
            ).scalars()
        )
    parts: list[str] = []
    for run in runs[-20:]:
        parts.append(f"User: {run.chat_message}")
        if run.final_answer:
            parts.append(f"Assistant: {run.final_answer}")
    history = "\n".join(parts)
    return history[-8000:]


async def load_settings_snapshot() -> dict[str, Any]:
    async with get_session_factory()() as session:
        return await get_settings(session)
