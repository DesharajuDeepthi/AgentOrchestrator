"""Direct sub-agent invocation (spec §7.5): the user pins the sub agent, so
the pin replaces the ROUTING decision — never the run lifecycle. A one-node
graph on the run's checkpointer thread reuses the ladder executor, so HITL
pause/resume, step recording, and worker code paths are identical to routed
dispatch; the runner's shared tail applies the formatter and finalization
exactly as for graph/agentic runs."""

from typing import Any, TypedDict

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.orchestrator.context import require_run_context

logger = structlog.get_logger("orchestrator.direct")

# the single dispatch slot on a direct run — worker thread ids and step
# node_ids both key off it, mirroring graph-mode plan-entry ids
DIRECT_ENTRY_ID = "direct"


class DirectState(TypedDict, total=False):
    task: str
    sub_agent_id: str
    # §15 eval: a single-skill ephemeral worker built by registry id — the
    # rung-4 exposure rule governs planner routing, never admin eval
    eval_skill_id: str
    is_eval: bool
    answer: str


class DirectInvokeError(RuntimeError):
    """The pinned sub agent cannot be invoked (gating or execution failure)."""


async def check_direct_invokable(
    sub_agent_id: str, *, allow_unexposed: bool = False
) -> dict[str, Any]:
    """Gating (spec §7.5): active + direct_exposure, from the registry cache.
    Returns the cached record; raises DirectInvokeError with the reason.
    §15 eval runs pass allow_unexposed — admin-direct eval is not gated by
    exposure, only by the definition being active."""
    from app.registry_cache import get_cache

    record = await get_cache().sub_agent_by_id(sub_agent_id)
    if record is None:
        raise DirectInvokeError(f"sub agent {sub_agent_id} not found")
    if record["status"] != "active":
        raise DirectInvokeError(f"sub agent {record['name']!r} is not active")
    if not record["direct_exposure"] and not allow_unexposed:
        raise DirectInvokeError(
            f"sub agent {record['name']!r} is not exposed for direct invocation"
        )
    return record


async def _eval_skill_resolution(skill_id: str) -> Any:
    """§15: build the single-skill ephemeral worker resolution directly by
    registry id — the same compile path §4 validation uses; hidden skills
    are evaluable, only 'active' is required."""
    from app.orchestrator.ladder import Resolution, ResolutionError
    from app.registry_cache import get_cache

    skill = await get_cache().skill_by_id(skill_id)
    if skill is None or skill["status"] != "active":
        raise ResolutionError(f"skill {skill_id} is not active")
    return Resolution(
        rung="dynamic_worker",
        tier="sub_agent",
        kind="dynamic",
        source="dynamic",
        entity_id=None,
        entity_name=f"eval-worker ({skill['name']})",
        payload={"skills": [skill], "callsign": "eval-worker"},
    )


async def invoke_node(state: DirectState) -> dict[str, Any]:
    """Resolve the pinned agent through the ladder's sub-agent rungs and run
    it with HITL propagation — the exact executor routed dispatch uses."""
    from app.orchestrator.graph_mode import RunFailed
    from app.orchestrator.ladder import execute_resolution, resolve_capability

    if state.get("eval_skill_id"):
        resolution = await _eval_skill_resolution(state["eval_skill_id"])
        result = await execute_resolution(resolution, state["task"], DIRECT_ENTRY_ID)
        if result.get("status") == "error":
            raise RunFailed(f"skill eval failed: {result.get('error')}")
        return {"answer": str(result.get("output") or "")}

    # defense in depth: re-check the gate at execution start — a toggle
    # flipped between request and execution fails the run cleanly
    try:
        await check_direct_invokable(
            state["sub_agent_id"], allow_unexposed=bool(state.get("is_eval"))
        )
    except DirectInvokeError as exc:
        raise RunFailed(str(exc)) from exc

    resolution = await resolve_capability({"type": "sub_agent", "id": state["sub_agent_id"]})
    result = await execute_resolution(resolution, state["task"], DIRECT_ENTRY_ID)
    if result.get("status") == "error":
        raise RunFailed(
            f"direct invocation of {resolution.entity_name!r} failed: {result.get('error')}"
        )
    return {"answer": str(result.get("output") or "")}


def build_direct_graph(
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph: StateGraph[DirectState, Any, Any, Any] = StateGraph(DirectState)
    graph.add_node("invoke", invoke_node)
    graph.add_edge(START, "invoke")
    graph.add_edge("invoke", END)
    return graph.compile(checkpointer=checkpointer)


async def summarize_history(conversation_id: Any) -> str | None:
    """Opt-in §7.5 history summary: ONE call on the default model at low
    effort over the same capped window the planner sees, recorded as a
    `summary` step with usage rolled up. Returns None when the conversation
    has no completed history (nothing to summarize) — the caller then runs
    the plain cold task. Fail-open: a summarization error logs, records the
    failed step, and the run proceeds without context rather than dying."""
    from app.llm import ModelParams, get_model, text_from_content
    from app.orchestrator.graph_mode import build_history
    from app.prompts import load_prompt
    from app.registry_cache import get_cache

    ctx = require_run_context()
    history = await build_history(conversation_id)
    if not history.strip():
        return None
    from app.orchestrator.context import ambient_default_override

    ref = str(ambient_default_override() or await get_cache().setting("default_model"))
    step_id = await ctx.recorder.start_step(
        "summary", tier="orchestrator", model=ref, input={"chars": len(history)}
    )
    try:
        model = get_model(ref, ModelParams(effort="low"))
        prompt = load_prompt("history_summary").format(history=history)
        ai = await model.ainvoke(prompt, config={"callbacks": ctx.callbacks})
        text = text_from_content(ai.content).strip()
        usage = getattr(ai, "usage_metadata", None) or {}
        await ctx.recorder.finish_step(
            step_id,
            output={"summary": text},
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        return text or None
    except Exception as exc:  # noqa: BLE001 - fail-open by design (spec §7.5)
        logger.warning("history_summary_failed", error=str(exc))
        await ctx.recorder.finish_step(step_id, status="failed", error=str(exc))
        return None


def compose_task_with_summary(summary: str, task: str) -> str:
    """The worker input shape (spec §7.5): summary block + verbatim request."""
    return f"Conversation summary (context):\n{summary}\n\nCurrent request: {task}"


async def record_direct_route(sub_agent_id: str) -> None:
    """One route step per direct run (spec §7.5) — recorded before the graph
    starts so HITL resume replays never duplicate it. Trace parity: the same
    rung names routed dispatch would log."""
    from app.orchestrator.ladder import resolve_capability

    ctx = require_run_context()
    resolution = await resolve_capability({"type": "sub_agent", "id": sub_agent_id})
    await ctx.recorder.record_route(
        capability={"type": "sub_agent", "id": sub_agent_id, "pinned": True},
        rung=resolution.rung,
        resolved_to={"entity_id": resolution.entity_id, "entity_name": resolution.entity_name},
        kind=resolution.kind,
        source=resolution.source,
    )
