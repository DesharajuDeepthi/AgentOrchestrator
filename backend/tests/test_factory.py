"""Worker factory (spec §6): DAG → compiled StateGraph — sequential, branch,
parallel + reachable joins, error edges, HITL, ephemeral multi-skill build,
persona merge order, tool isolation, max_tool_iterations."""

from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.factory.worker import (
    NodeExecutionError,
    assemble_skill_prompt,
    build_ephemeral_snapshot,
    build_worker,
    compiled_worker_cache_info,
    get_compiled_worker,
)
from app.llm import fake as fake_llm
from app.models import Skill
from app.native.provider import native_tool as _nt
from tests.factory_helpers import create_skill, create_sub_agent, create_tool, load_snapshot

if "bound_tool" not in __import__("app.native.provider", fromlist=["native_tools"]).native_tools():

    @_nt("bound_tool", "test-only bound tool")
    async def _bound_tool_fn() -> str:
        return "bound result"

    @_nt("unbound_tool", "test-only unbound tool")
    async def _unbound_tool_fn() -> str:
        return "unbound result"

    @_nt("loop_tool", "test-only looping tool")
    async def _loop_tool_fn() -> str:
        return "loop result"


@pytest.fixture(autouse=True)
async def _fake_default_model() -> None:
    from app.db import get_session_factory
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(session, {"default_model": "fake:scripted"})


@pytest.fixture
def checkpointer() -> MemorySaver:
    return MemorySaver()


def run_config(thread: str | None = None) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread or uuid4().hex}}


async def invoke(worker: Any, task: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = await worker.ainvoke(
        {"task": task, "messages": []}, config=config or run_config()
    )
    return result


class TestSequential:
    async def test_two_skill_chain(self, checkpointer: MemorySaver) -> None:
        s1 = await create_skill(name="first")
        s2 = await create_skill(name="second")
        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "a", "type": "skill", "skill_id": str(s1.id)},
                    {"id": "b", "type": "skill", "skill_id": str(s2.id)},
                ],
                "edges": [
                    {"from": "START", "to": "a"},
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "END"},
                ],
            }
        )
        snapshot = await load_snapshot(agent.id)
        worker = build_worker(snapshot, checkpointer=checkpointer)
        fake_llm.push_ai("alpha output")
        fake_llm.push_ai("beta output")
        state = await invoke(worker, "do the thing")
        assert state["node_outputs"]["a"]["status"] == "ok"
        assert state["node_outputs"]["a"]["output"] == "alpha output"
        assert state["node_outputs"]["b"]["output"] == "beta output"
        # downstream node received the prior node's output in its context
        assert state["node_outputs"]["b"]["status"] == "ok"

    async def test_node_output_usage_recorded(self, checkpointer: MemorySaver) -> None:
        s1 = await create_skill(name="usage-skill")
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "a", "type": "skill", "skill_id": str(s1.id)}],
                "edges": [{"from": "START", "to": "a"}, {"from": "a", "to": "END"}],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.push_ai("done")
        state = await invoke(worker, "count tokens")
        usage = state["node_outputs"]["a"]["usage"]
        assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0


class TestBranching:
    async def make_branch_agent(self) -> Any:
        s1 = await create_skill(name="research-like")
        s2 = await create_skill(name="write-like")
        return await create_sub_agent(
            {
                "nodes": [
                    {"id": "research", "type": "skill", "skill_id": str(s1.id)},
                    {"id": "write", "type": "skill", "skill_id": str(s2.id)},
                ],
                "edges": [
                    {"from": "START", "to": "research"},
                    {"from": "research", "to": "write", "condition": "if results were found"},
                    {"from": "research", "to": "END", "condition": "if nothing was found"},
                    {"from": "write", "to": "END"},
                ],
            }
        )

    async def test_router_selects_first_condition(self, checkpointer: MemorySaver) -> None:
        agent = await self.make_branch_agent()
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.push_ai("found lots of results")
        # router structured-output call chooses edge 0
        fake_llm.push_ai(
            "", tool_calls=[{"name": "ConditionChoice", "args": {"index": 0}, "id": "r1"}]
        )
        fake_llm.push_ai("wrote it up")
        state = await invoke(worker, "branch task")
        assert state["node_outputs"]["write"]["output"] == "wrote it up"
        route = state["node_outputs"]["route:research"]
        assert route["chosen"] == "write"

    async def test_router_selects_end_branch(self, checkpointer: MemorySaver) -> None:
        agent = await self.make_branch_agent()
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.push_ai("nothing found")
        fake_llm.push_ai(
            "", tool_calls=[{"name": "ConditionChoice", "args": {"index": 1}, "id": "r1"}]
        )
        state = await invoke(worker, "branch task")
        assert "write" not in state["node_outputs"]  # skipped branch never ran


class TestErrorEdges:
    async def test_error_edge_taken_on_node_failure(self, checkpointer: MemorySaver) -> None:
        s1 = await create_skill(name="fragile")
        s2 = await create_skill(name="recovery")
        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "frag", "type": "skill", "skill_id": str(s1.id)},
                    {"id": "rescue", "type": "skill", "skill_id": str(s2.id)},
                ],
                "edges": [
                    {"from": "START", "to": "frag"},
                    {"from": "frag", "to": "END"},
                    {"from": "frag", "to": "rescue", "on": "error"},
                    {"from": "rescue", "to": "END"},
                ],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.push_error(RuntimeError("model exploded"))
        fake_llm.push_ai("recovered gracefully")
        state = await invoke(worker, "error path")
        assert state["node_outputs"]["frag"]["status"] == "error"
        assert "model exploded" in state["node_outputs"]["frag"]["error"]
        assert state["node_outputs"]["rescue"]["status"] == "ok"
        # the failed node's error text lands in state for the downstream node
        assert state["node_outputs"]["rescue"]["output"] == "recovered gracefully"

    async def test_no_error_edge_fails_run(self, checkpointer: MemorySaver) -> None:
        s1 = await create_skill(name="fragile-fatal")
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "frag", "type": "skill", "skill_id": str(s1.id)}],
                "edges": [{"from": "START", "to": "frag"}, {"from": "frag", "to": "END"}],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.push_error(RuntimeError("fatal model error"))
        with pytest.raises(NodeExecutionError, match="frag"):
            await invoke(worker, "fatal path")


class TestParallel:
    async def test_fan_out_join_completes_both(self, checkpointer: MemorySaver) -> None:
        s = await create_skill(name="par")
        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "start_node", "type": "skill", "skill_id": str(s.id)},
                    {"id": "left", "type": "skill", "skill_id": str(s.id)},
                    {"id": "right", "type": "skill", "skill_id": str(s.id)},
                    {"id": "join", "type": "skill", "skill_id": str(s.id)},
                ],
                "edges": [
                    {"from": "START", "to": "start_node"},
                    {"from": "start_node", "to": "left"},
                    {"from": "start_node", "to": "right"},
                    {"from": "left", "to": "join"},
                    {"from": "right", "to": "join"},
                    {"from": "join", "to": "END"},
                ],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        for text in ["s out", "L out", "R out", "J out"]:
            fake_llm.push_ai(text)
        state = await invoke(worker, "parallel task")
        outs = state["node_outputs"]
        assert {"start_node", "left", "right", "join"} <= set(outs)
        assert outs["join"]["status"] == "ok"

    async def test_reachable_join_no_deadlock(self, checkpointer: MemorySaver) -> None:
        """Join has two incoming edges but only one branch is taken —
        reachable-join semantics must not deadlock (spec §3.5)."""
        s = await create_skill(name="reach")
        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "gate", "type": "skill", "skill_id": str(s.id)},
                    {"id": "taken", "type": "skill", "skill_id": str(s.id)},
                    {"id": "skipped", "type": "skill", "skill_id": str(s.id)},
                    {"id": "join", "type": "skill", "skill_id": str(s.id)},
                ],
                "edges": [
                    {"from": "START", "to": "gate"},
                    {"from": "gate", "to": "taken", "condition": "go left"},
                    {"from": "gate", "to": "skipped", "condition": "go right"},
                    {"from": "taken", "to": "join"},
                    {"from": "skipped", "to": "join"},
                    {"from": "join", "to": "END"},
                ],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.push_ai("gate out")
        fake_llm.push_ai(
            "", tool_calls=[{"name": "ConditionChoice", "args": {"index": 0}, "id": "r"}]
        )
        fake_llm.push_ai("taken out")
        fake_llm.push_ai("join out")
        state = await invoke(worker, "reachable join")
        outs = state["node_outputs"]
        assert outs["join"]["status"] == "ok"
        assert "skipped" not in outs


class TestHitl:
    async def make_hitl_worker(self, checkpointer: MemorySaver) -> Any:
        s1 = await create_skill(name="pre-hitl")
        s2 = await create_skill(name="post-hitl")
        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "work", "type": "skill", "skill_id": str(s1.id)},
                    {"id": "gate", "type": "hitl", "prompt": "Proceed with save?"},
                    {"id": "save", "type": "skill", "skill_id": str(s2.id)},
                ],
                "edges": [
                    {"from": "START", "to": "work"},
                    {"from": "work", "to": "gate"},
                    {"from": "gate", "to": "save"},
                    {"from": "save", "to": "END"},
                ],
            }
        )
        return build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)

    async def test_interrupt_pause_and_approve(self, checkpointer: MemorySaver) -> None:
        worker = await self.make_hitl_worker(checkpointer)
        config = run_config("hitl-approve")
        fake_llm.push_ai("work done")
        state = await invoke(worker, "hitl task", config)
        interrupts = state["__interrupt__"]
        assert interrupts[0].value["prompt"] == "Proceed with save?"

        fake_llm.push_ai("saved")
        resumed: dict[str, Any] = await worker.ainvoke(
            Command(resume={"decision": "approve", "note": "go ahead"}), config=config
        )
        assert resumed["node_outputs"]["gate"]["status"] == "ok"
        assert resumed["node_outputs"]["save"]["output"] == "saved"

    async def test_interrupt_deny_routes_to_end(self, checkpointer: MemorySaver) -> None:
        worker = await self.make_hitl_worker(checkpointer)
        config = run_config("hitl-deny")
        fake_llm.push_ai("work done")
        await invoke(worker, "hitl task", config)
        resumed: dict[str, Any] = await worker.ainvoke(
            Command(resume={"decision": "deny", "note": "not now"}), config=config
        )
        gate = resumed["node_outputs"]["gate"]
        assert gate["status"] == "denied"
        assert "not now" in gate["note"]
        assert "save" not in resumed["node_outputs"]  # denial note in state, node skipped


class TestEphemeral:
    async def test_multi_skill_sequential_chain(self, checkpointer: MemorySaver) -> None:
        s1 = await create_skill(name="lead", persona="LEAD PERSONA.")
        s2 = await create_skill(name="follow", persona="FOLLOW PERSONA.")
        snapshot = build_ephemeral_snapshot(
            [await _reload(s1), await _reload(s2)], task="chained work"
        )
        assert snapshot["sub_agent"]["kind"] == "dynamic"
        assert snapshot["sub_agent"]["id"] is None
        worker = build_worker(snapshot, checkpointer=checkpointer)
        fake_llm.push_ai("lead done")
        fake_llm.push_ai("follow done")
        state = await invoke(worker, "chained work")
        outs = {k: v for k, v in state["node_outputs"].items() if not k.startswith("route:")}
        assert [outs[k]["output"] for k in sorted(outs)] == ["lead done", "follow done"]


class TestPromptAssembly:
    def test_persona_merge_order(self) -> None:
        prompt = assemble_skill_prompt(
            sub_agent_persona="AGENT PERSONA",
            skill_persona="SKILL PERSONA",
            skill_instructions="SKILL INSTRUCTIONS",
            node_instructions="NODE ADDENDUM",
        )
        order = [
            prompt.index("AGENT PERSONA"),
            prompt.index("SKILL PERSONA"),
            prompt.index("SKILL INSTRUCTIONS"),
            prompt.index("NODE ADDENDUM"),
        ]
        assert order == sorted(order)
        # (5) tool-usage guidance comes last
        assert prompt.rstrip().lower().rindex("tool") > order[-1]

    def test_lead_persona_for_ephemeral_follows(self) -> None:
        prompt = assemble_skill_prompt(
            sub_agent_persona="LEAD PERSONA",
            skill_persona="FOLLOW PERSONA",
            skill_instructions="I",
            node_instructions=None,
        )
        assert prompt.index("LEAD PERSONA") < prompt.index("FOLLOW PERSONA")


class TestToolIsolationAndLimits:
    async def test_skill_loop_sees_exactly_bound_tools(self, checkpointer: MemorySaver) -> None:
        bound = await create_tool(tool_name="bound_tool", tool_key="bound.tool")
        await create_tool(tool_name="unbound_tool", tool_key="unbound.tool")
        skill = await create_skill(name="isolated", tools=[bound])
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "iso", "type": "skill", "skill_id": str(skill.id)}],
                "edges": [{"from": "START", "to": "iso"}, {"from": "iso", "to": "END"}],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        fake_llm.clear_seen_tools()
        fake_llm.push_ai("done")
        await invoke(worker, "isolation")
        seen = fake_llm.seen_tools()
        assert seen, "model call should have been captured"
        assert seen[-1] == ["bound_tool"]  # nothing else, ever (spec §3.3)

    async def test_max_tool_iterations_exceeded_is_node_error(
        self, checkpointer: MemorySaver
    ) -> None:
        from app.db import get_session_factory
        from app.settings_store import update_settings

        async with get_session_factory()() as session:
            await update_settings(session, {"max_tool_iterations": 1})

        tool = await create_tool(tool_name="loop_tool", tool_key="loop.tool")
        skill = await create_skill(name="looper", tools=[tool])
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "n", "type": "skill", "skill_id": str(skill.id)}],
                "edges": [{"from": "START", "to": "n"}, {"from": "n", "to": "END"}],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        for _ in range(5):
            fake_llm.push_ai(
                "", tool_calls=[{"name": "loop_tool", "args": {}, "id": f"c{uuid4().hex[:4]}"}]
            )
        with pytest.raises(NodeExecutionError, match="limit"):
            await invoke(worker, "loop forever")


class TestCache:
    async def test_compiled_cache_keyed_by_updated_at(self, checkpointer: MemorySaver) -> None:
        s = await create_skill(name="cached")
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "n", "type": "skill", "skill_id": str(s.id)}],
                "edges": [{"from": "START", "to": "n"}, {"from": "n", "to": "END"}],
            }
        )
        snap = await load_snapshot(agent.id)
        w1 = get_compiled_worker(snap, checkpointer=checkpointer)
        w2 = get_compiled_worker(snap, checkpointer=checkpointer)
        assert w1 is w2
        snap2 = dict(snap)
        snap2["sub_agent"] = dict(snap["sub_agent"], updated_at="2099-01-01T00:00:00")
        w3 = get_compiled_worker(snap2, checkpointer=checkpointer)
        assert w3 is not w1
        assert compiled_worker_cache_info()["size"] >= 2


async def _reload(skill: Skill) -> dict[str, Any]:
    from app.db import get_session_factory
    from app.factory.worker import snapshot_skill

    async with get_session_factory()() as session:
        merged = await session.merge(skill)
        await session.refresh(merged)
        return await snapshot_skill(session, merged)


class TestCompileAtSave:
    async def test_router_namespace_collision_rejected_at_save(self, client: Any) -> None:
        """A node id colliding with the factory's synthesized `__route__<id>`
        namespace is rejected at save (spec §6). It used to reach the factory
        and fail there; it is now caught by structural validation, one step
        earlier and with a message naming the actual problem."""
        from httpx import AsyncClient

        assert isinstance(client, AsyncClient)
        skill = await create_skill(name="compile-skill")
        workflow = {
            "nodes": [
                {"id": "n1", "type": "skill", "skill_id": str(skill.id)},
                {"id": "__route__n1", "type": "skill", "skill_id": str(skill.id)},
            ],
            "edges": [
                {"from": "START", "to": "n1"},
                {"from": "n1", "to": "__route__n1"},
                {"from": "__route__n1", "to": "END"},
            ],
        }
        resp = await client.post(
            "/api/v1/sub-agents",
            json={
                "name": "uncompilable",
                "description": "x",
                "persona": "p",
                "workflow": workflow,
            },
        )
        assert resp.status_code == 422
        assert "reserved" in resp.text.lower()

    async def test_uncompilable_snapshot_surfaces_as_compile_error(self) -> None:
        """compile-time = save-time (spec §6): a DAG the factory cannot build
        is reported as an error rather than raising out of the check."""
        from app.db import get_session_factory
        from app.factory.worker import build_worker, compile_workflow_check

        skill = await create_skill(name="orphan-skill")
        workflow = {
            "nodes": [{"id": "n1", "type": "skill", "skill_id": str(skill.id)}],
            "edges": [{"from": "START", "to": "n1"}, {"from": "n1", "to": "END"}],
        }
        # the factory raises when a skill node is missing from the snapshot
        with pytest.raises(ValueError, match="not in snapshot"):
            build_worker({"sub_agent": {}, "workflow": workflow, "skills": {}}, None)
        # and the save-time check reports it instead of propagating
        async with get_session_factory()() as session:
            errors = await compile_workflow_check(
                session, {"nodes": workflow["nodes"], "edges": []}
            )
        assert errors, "an unbuildable workflow must produce compile errors"


class TestPostgresCheckpointer:
    async def test_hitl_resume_over_postgres(self) -> None:
        """HITL pause/resume rides the LangGraph Postgres checkpointer (spec §6)."""
        from app.db import get_checkpointer

        checkpointer = await get_checkpointer()
        s1 = await create_skill(name="pg-pre")
        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "work", "type": "skill", "skill_id": str(s1.id)},
                    {"id": "gate", "type": "hitl", "prompt": "Continue?"},
                ],
                "edges": [
                    {"from": "START", "to": "work"},
                    {"from": "work", "to": "gate"},
                    {"from": "gate", "to": "END"},
                ],
            }
        )
        worker = build_worker(await load_snapshot(agent.id), checkpointer=checkpointer)
        config = {"configurable": {"thread_id": f"pg-{uuid4().hex}"}}
        fake_llm.push_ai("pg work done")
        state = await worker.ainvoke({"task": "pg hitl", "messages": []}, config=config)
        assert state["__interrupt__"][0].value["prompt"] == "Continue?"
        resumed = await worker.ainvoke(
            Command(resume={"decision": "approve", "note": ""}), config=config
        )
        assert resumed["node_outputs"]["gate"]["status"] == "ok"


class TestNativeSubAgent:
    async def test_registered_native_agent_invocable(self) -> None:
        """Native sub agents bypass the factory: the registered build callable
        returns a compiled graph over the standard state schema (spec §3.4)."""
        from langgraph.graph import END, START, StateGraph

        from app.factory.worker import WorkerState, get_native_worker
        from app.native.provider import native_sub_agent, native_sub_agents

        if "test-native-agent" not in native_sub_agents():

            @native_sub_agent("test-native-agent", "a test native agent")
            def build_test_agent(checkpointer: Any) -> Any:
                async def do_work(state: WorkerState) -> dict[str, Any]:
                    return {"node_outputs": {"native-step": {"status": "ok", "output": "native!"}}}

                g = StateGraph(WorkerState)
                g.add_node("native-step", do_work)
                g.add_edge(START, "native-step")
                g.add_edge("native-step", END)
                return g.compile(checkpointer=checkpointer)

        worker = get_native_worker("test-native-agent", checkpointer=None)
        state = await worker.ainvoke({"task": "hello", "messages": []})
        assert state["node_outputs"]["native-step"]["output"] == "native!"
