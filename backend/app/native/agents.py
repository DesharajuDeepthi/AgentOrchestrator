"""Native sub agents (spec §3.4): hand-written compiled graphs, registered
at startup. The worker factory is bypassed for graph CONSTRUCTION — the DAG
below is code, not a workflow record — but each skill node delegates to the
factory's node semantics, so scoped tool binding, model resolution, and the
middleware stack behave exactly as in factory-built workers.
"""

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.native.provider import native_sub_agent

logger = structlog.get_logger("native.agents")

# the warden's stage-level persona, applied over each skill's own persona
_WARDEN_PERSONA = (
    "You are the workspace warden: audit first, then tidy. Ground every "
    "action in what the audit actually found."
)


def _skill_stage(node_id: str, skill_name: str) -> Any:
    """A graph node that runs a registry skill by NAME, resolved live from
    the cache at execution time — native code never hard-wires registry ids."""

    async def run_stage(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        from app.factory.worker import _make_skill_node
        from app.registry_cache import get_cache

        rows = await get_cache().skills(exposed_only=False)
        snapshot = next((s for s in rows if s["name"] == skill_name), None)
        if snapshot is None:
            logger.warning("native_stage_skill_missing", node_id=node_id, skill=skill_name)
            return {
                "node_outputs": {
                    node_id: {
                        "status": "error",
                        "error": f"skill {skill_name!r} is not in the registry or not active",
                    }
                }
            }
        inner = _make_skill_node(
            {"id": node_id}, {"sub_agent": {"persona": _WARDEN_PERSONA}}, snapshot
        )
        result: dict[str, Any] = await inner(state, config)
        return result

    return run_stage


@native_sub_agent(
    "workspace-warden",
    "Audits the sandboxed workspace, then tidies it: a code-defined two-stage "
    "graph (audit → curate) over the workspace-auditor and workspace-curator "
    "skills. Use for any 'inspect and organize the workspace' request.",
    # skill NAMES here — the seed pass resolves them to registry uuids
    covers_skill_ids=["workspace-auditor", "workspace-curator"],
)
def build_workspace_warden(checkpointer: Any) -> Any:
    from app.factory.worker import WorkerState

    graph: StateGraph[Any, Any, Any, Any] = StateGraph(WorkerState)
    graph.add_node("audit", _skill_stage("audit", "workspace-auditor"))
    graph.add_node("curate", _skill_stage("curate", "workspace-curator"))
    graph.add_edge(START, "audit")
    graph.add_edge("audit", "curate")
    graph.add_edge("curate", END)
    return graph.compile(checkpointer=checkpointer)
