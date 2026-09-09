"""Orchestrator API contract (spec §7, §11): chat → run → HITL happy path,
capability resolution ladder (one test per rung + precedence), plan
validate/repair, full-catalog fallback + isolation, cancel/retry, both modes
over the same fixture registries, SSE contract, answer UI, /metrics."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import RunStep, Tool
from app.native.provider import native_sub_agent, native_sub_agents, native_tool, native_tools
from app.settings_store import update_settings
from tests.factory_helpers import create_skill, create_sub_agent, create_tool

API = "/api/v1"

# ── test-only native tools ───────────────────────────────────────

if "orch_echo" not in native_tools():

    @native_tool("orch_echo", "echo a message back")
    async def _orch_echo(message: str) -> str:
        return f"orch-echo:{message}"

    @native_tool("orch_slow", "sleeps then returns")
    async def _orch_slow() -> str:
        await asyncio.sleep(3)
        return "slow done"

    @native_tool("orch_inserter", "inserts a new exposed tool row when called")
    async def _orch_inserter() -> str:
        async with get_session_factory()() as session:
            session.add(
                Tool(
                    name="midloop_tool",
                    kind="native",
                    tool_name="orch_echo",
                    tool_key=f"midloop-{uuid4().hex[:6]}",
                    source="dynamic",
                    direct_exposure=True,
                    input_schema={"type": "object", "properties": {}},
                )
            )
            await session.commit()
        from app.registry_cache import get_cache

        await get_cache().invalidate("tools")
        return "inserted"


@pytest.fixture(autouse=True, params=["bypass", "memory"])
async def _registry_cache_mode(request: pytest.FixtureRequest) -> None:
    """Spec §11: the orchestrator/middleware/ladder suite runs in both cache
    modes — identical observable behavior is the §7.3 no-degradation gate."""
    async with get_session_factory()() as session:
        await update_settings(session, {"registry_cache_mode": request.param})


@pytest.fixture(autouse=True)
async def _orchestrator_settings() -> None:
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "default_model": "fake:scripted",
                "formatter_enabled": False,
                "orchestrator_mode": "graph",
            },
        )


def plan_call(
    entries: list[dict[str, Any]] | None = None,
    direct_answer: str | None = None,
    no_confident_match: bool = False,
) -> None:
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "PlannerOutput",
                "args": {
                    "entries": entries or [],
                    "direct_answer": direct_answer,
                    "no_confident_match": no_confident_match,
                },
                "id": f"p{uuid4().hex[:6]}",
            }
        ],
    )


async def send_chat(client: AsyncClient, message: str = "do the thing") -> str:
    resp = await client.post(f"{API}/chat", json={"message": message})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["run_id"])


async def wait_run(
    client: AsyncClient, run_id: str, statuses: set[str], timeout_s: float = 20.0
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        run = (await client.get(f"{API}/runs/{run_id}")).json()
        if run["status"] in statuses:
            return dict(run)
        await asyncio.sleep(0.1)
    raise AssertionError(f"run did not reach {statuses}; last: {run['status']}")


def steps_of_type(run: dict[str, Any], step_type: str) -> list[dict[str, Any]]:
    return [s for s in run["steps"] if s["step_type"] == step_type]


def route_rungs(run: dict[str, Any]) -> list[str]:
    return [s["output"].get("rung") for s in steps_of_type(run, "route") if s.get("output")]


class TestGraphModeBasics:
    async def test_direct_answer_no_capability(self, client: AsyncClient) -> None:
        plan_call(direct_answer="Hello! Nothing to dispatch.")
        run_id = await send_chat(client, "hello")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed"
        assert run["final_answer"] == "Hello! Nothing to dispatch."
        assert run["orchestrator_mode"] == "graph"
        assert steps_of_type(run, "plan")
        assert run["total_output_tokens"] > 0

    async def test_multi_turn_history_reaches_planner(self, client: AsyncClient) -> None:
        plan_call(direct_answer="first answer")
        run_id = await send_chat(client, "first message")
        run = await wait_run(client, run_id, {"completed"})
        conversation_id = run["conversation_id"]
        plan_call(direct_answer="second answer")
        resp = await client.post(
            f"{API}/chat",
            json={"conversation_id": conversation_id, "message": "follow-up"},
        )
        run2 = await wait_run(client, str(resp.json()["run_id"]), {"completed"})
        assert run2["conversation_id"] == conversation_id
        conv = (await client.get(f"{API}/conversations/{conversation_id}")).json()
        contents = [m["content"] for m in conv["messages"]]
        assert contents == ["first message", "first answer", "follow-up", "second answer"]

    async def test_direct_answer_plus_entries_both_execute(self, client: AsyncClient) -> None:
        """A plan may answer a trivial part directly AND dispatch entries for
        the rest — the entries must still run, and the direct part must be
        merged by the aggregator rather than short-circuiting the run."""
        skill = await create_skill(name=f"mix-{uuid4().hex[:4]}", direct_exposure=True)
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "do part two",
                    "depends_on": [],
                }
            ],
            direct_answer="part one handled directly",
        )
        fake_llm.push_ai("part two result")
        fake_llm.push_ai("Merged: both parts")
        run_id = await send_chat(client, "two part request")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "direct_skill" in route_rungs(run), "the planned entry must still dispatch"
        assert run["final_answer"] == "Merged: both parts"
        assert steps_of_type(run, "aggregate"), "aggregator must merge, not short-circuit"


class TestResolutionLadder:
    async def test_rung1_direct_tool(self, client: AsyncClient) -> None:
        from app.factory.worker import sanitize_tool_name

        tool = await create_tool(
            tool_name="orch_echo", tool_key=f"echo-{uuid4().hex[:4]}", direct_exposure=True
        )
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_tool", "id": str(tool.id)},
                    "task": "echo hello",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": sanitize_tool_name(tool.tool_key),
                    "args": {"message": "hello"},
                    "id": "t1",
                }
            ],
        )
        fake_llm.push_ai("Echo result delivered.")  # aggregator
        run_id = await send_chat(client, "use the echo tool")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "direct_tool" in route_rungs(run)
        tool_steps = steps_of_type(run, "tool_call")
        assert any("orch-echo:hello" in str(s.get("output")) for s in tool_steps)

    async def test_rung1_direct_skill(self, client: AsyncClient) -> None:
        skill = await create_skill(name=f"exposed-{uuid4().hex[:4]}", direct_exposure=True)
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "run the skill",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("inline skill result")
        fake_llm.push_ai("Aggregated: inline skill result")
        run_id = await send_chat(client)
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "direct_skill" in route_rungs(run)
        assert any(s["status"] == "completed" for s in steps_of_type(run, "skill"))

    async def test_rung2_native_covers_skill(self, client: AsyncClient) -> None:
        skill = await create_skill(name=f"covered-{uuid4().hex[:4]}")
        agent_name = f"native-cover-{uuid4().hex[:4]}"

        @native_sub_agent(agent_name, "covers the skill", covers_skill_ids=[str(skill.id)])
        def build_cover(checkpointer: Any) -> Any:
            from langgraph.graph import END, START, StateGraph

            from app.factory.worker import WorkerState

            async def work(state: WorkerState) -> dict[str, Any]:
                return {
                    "node_outputs": {"native-work": {"status": "ok", "output": "native handled it"}}
                }

            g = StateGraph(WorkerState)
            g.add_node("native-work", work)
            g.add_edge(START, "native-work")
            g.add_edge("native-work", END)
            return g.compile(checkpointer=checkpointer)

        from app.seed.loader import upsert_native_sub_agents

        async with get_session_factory()() as session:
            await upsert_native_sub_agents(session)

        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "do covered work",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("Aggregated: native handled it")
        run_id = await send_chat(client)
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "native_sub_agent" in route_rungs(run)
        native_sub_agents().pop(agent_name, None)

    async def test_rung3_custom_sub_agent(self, client: AsyncClient) -> None:
        skill = await create_skill(name=f"cust-{uuid4().hex[:4]}")
        await create_sub_agent(
            {
                "nodes": [{"id": "n", "type": "skill", "skill_id": str(skill.id)}],
                "edges": [{"from": "START", "to": "n"}, {"from": "n", "to": "END"}],
            },
            name=f"custom-cover-{uuid4().hex[:4]}",
        )
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "do custom work",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("custom worker node output")
        fake_llm.push_ai("Aggregated: custom output")
        run_id = await send_chat(client)
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "custom_sub_agent" in route_rungs(run)

    async def test_rung4_ephemeral_dynamic_worker(self, client: AsyncClient) -> None:
        # rung 4 is reached by NAMING skills (spec §7.1): the planner emits a
        # spin_worker entry over exposed skills no sub agent covers
        skill = await create_skill(name=f"lonely-{uuid4().hex[:4]}", direct_exposure=True)
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "spin_worker", "skill_ids": [str(skill.id)]},
                    "task": "nobody covers this",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("ephemeral output")
        fake_llm.push_ai("Aggregated: ephemeral output")
        run_id = await send_chat(client)
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "dynamic_worker" in route_rungs(run)
        route = next(
            s for s in steps_of_type(run, "route") if s["output"]["rung"] == "dynamic_worker"
        )
        assert route["output"]["resolved_to"]["kind"] == "dynamic"
        assert route["output"]["resolved_to"]["entity_id"] is None

    async def test_precedence_direct_beats_native_beats_custom(self, client: AsyncClient) -> None:
        skill = await create_skill(name=f"prec-{uuid4().hex[:4]}", direct_exposure=True)
        agent_name = f"native-prec-{uuid4().hex[:4]}"

        @native_sub_agent(agent_name, "also covers", covers_skill_ids=[str(skill.id)])
        def build_prec(checkpointer: Any) -> Any:
            from langgraph.graph import END, START, StateGraph

            from app.factory.worker import WorkerState

            async def work(state: WorkerState) -> dict[str, Any]:
                return {"node_outputs": {"w": {"status": "ok", "output": "native"}}}

            g = StateGraph(WorkerState)
            g.add_node("w", work)
            g.add_edge(START, "w")
            g.add_edge("w", END)
            return g.compile(checkpointer=checkpointer)

        from app.seed.loader import upsert_native_sub_agents

        async with get_session_factory()() as session:
            await upsert_native_sub_agents(session)
        await create_sub_agent(
            {
                "nodes": [{"id": "n", "type": "skill", "skill_id": str(skill.id)}],
                "edges": [{"from": "START", "to": "n"}, {"from": "n", "to": "END"}],
            },
            name=f"custom-prec-{uuid4().hex[:4]}",
        )

        # exposed → rung 1 wins
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "t",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("inline result")
        fake_llm.push_ai("agg")
        run = await wait_run(client, await send_chat(client), {"completed", "failed"})
        assert "direct_skill" in route_rungs(run)

        # unexposed → native (rung 2) beats custom (rung 3)
        async with get_session_factory()() as session:
            from app.models import Skill as SkillModel

            row = await session.get(SkillModel, skill.id)
            assert row is not None
            row.direct_exposure = False
            await session.commit()
        from app.registry_cache import get_cache

        await get_cache().invalidate("skills")
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "t",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("agg2")
        run = await wait_run(client, await send_chat(client), {"completed", "failed"})
        assert "native_sub_agent" in route_rungs(run)
        native_sub_agents().pop(agent_name, None)


class TestPlanRepairAndFailure:
    async def test_invalid_then_repaired(self, client: AsyncClient) -> None:
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "sub_agent", "id": str(uuid4())},
                    "task": "t",
                    "depends_on": [],
                }
            ]
        )
        plan_call(direct_answer="repaired fine")
        run_id = await send_chat(client)
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed"
        assert run["final_answer"] == "repaired fine"

    async def test_spin_worker_over_hidden_skill_is_a_validation_error(self) -> None:
        """spec §7.1 rung 4: plan validation rejects a spin_worker entry over a
        non-exposed skill, so the planner gets a repair round instead of a
        mid-run resolution failure."""
        from app.db import get_session_factory
        from app.orchestrator.planner import PlannerOutput, validate_plan

        hidden = await create_skill(name=f"planhidden-{uuid4().hex[:6]}")
        shown = await create_skill(name=f"planshown-{uuid4().hex[:6]}", direct_exposure=True)
        plan = PlannerOutput.model_validate(
            {
                "entries": [
                    {
                        "id": "s1",
                        "capability": {
                            "type": "spin_worker",
                            "skill_ids": [str(shown.id), str(hidden.id)],
                        },
                        "task": "t",
                        "depends_on": [],
                    }
                ]
            }
        )
        async with get_session_factory()() as session:
            errors = await validate_plan(session, plan, 5)
        assert any("not exposed to you" in e for e in errors)
        assert not any(str(shown.id) in e for e in errors)

    async def test_invalid_twice_fails_with_raw_outputs(self, client: AsyncClient) -> None:
        bogus = str(uuid4())
        for _ in range(2):
            plan_call(
                entries=[
                    {
                        "id": "s1",
                        "capability": {"type": "sub_agent", "id": bogus},
                        "task": "t",
                        "depends_on": [],
                    }
                ]
            )
        run_id = await send_chat(client)
        run = await wait_run(client, run_id, {"failed"})
        assert "plan" in str(run["error"]).lower()
        assert len(run["plan"]["raw_planner_outputs"]) == 2


class TestHitlHappyPath:
    async def make_hitl_agent(self) -> Any:
        s1 = await create_skill(name=f"pre-{uuid4().hex[:4]}")
        s2 = await create_skill(name=f"post-{uuid4().hex[:4]}")
        return await create_sub_agent(
            {
                "nodes": [
                    {"id": "work", "type": "skill", "skill_id": str(s1.id)},
                    {"id": "gate", "type": "hitl", "prompt": "Save the result?"},
                    {"id": "save", "type": "skill", "skill_id": str(s2.id)},
                ],
                "edges": [
                    {"from": "START", "to": "work"},
                    {"from": "work", "to": "gate"},
                    {"from": "gate", "to": "save"},
                    {"from": "save", "to": "END"},
                ],
            },
            name=f"hitl-agent-{uuid4().hex[:4]}",
        )

    async def test_pause_pending_approve_resume(self, client: AsyncClient) -> None:
        agent = await self.make_hitl_agent()
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "sub_agent", "id": str(agent.id)},
                    "task": "work then save",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("work output")
        run_id = await send_chat(client, "hitl flow")
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]

        pending = (await client.get(f"{API}/hitl/pending")).json()
        assert any(p["run_id"] == run_id for p in pending)

        fake_llm.push_ai("save output")
        fake_llm.push_ai("Aggregated: saved")
        resp = await client.post(
            f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": "go"}
        )
        assert resp.status_code == 200
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]

        hitl_steps = steps_of_type(run, "hitl")
        assert len(hitl_steps) == 1
        work_steps = [s for s in steps_of_type(run, "skill") if s.get("node_id") == "work"]
        assert len(work_steps) == 1  # idempotent replay — no duplicate dispatch

    async def test_deny_routes_to_end(self, client: AsyncClient) -> None:
        agent = await self.make_hitl_agent()
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "sub_agent", "id": str(agent.id)},
                    "task": "work then save",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("work output")
        run_id = await send_chat(client, "deny flow")
        await wait_run(client, run_id, {"paused_hitl"})
        fake_llm.push_ai("Aggregated despite denial")
        await client.post(f"{API}/runs/{run_id}/hitl", json={"decision": "deny", "note": "no"})
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert not any(s.get("node_id") == "save" for s in run["steps"])


class TestFallback:
    async def test_no_confident_match_uses_full_catalog(self, client: AsyncClient) -> None:
        unexposed = await create_tool(
            tool_name="orch_echo", tool_key=f"hidden-{uuid4().hex[:4]}", direct_exposure=False
        )
        plan_call(no_confident_match=True)
        fake_llm.clear_seen_tools()
        fake_llm.push_ai("fallback handled it")  # fallback loop answers directly
        fake_llm.push_ai("Aggregated: fallback handled it")
        run_id = await send_chat(client, "something nothing covers")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "fallback" in route_rungs(run)
        from app.factory.worker import sanitize_tool_name

        seen = [names for names in fake_llm.seen_tools() if names]
        assert any(sanitize_tool_name(unexposed.tool_key) in names for names in seen), (
            "full catalog must include unexposed tools"
        )

    async def test_duplicate_skill_names_bind_unique_tool_names(self, client: AsyncClient) -> None:
        """Registry names need not be unique — bound tool names must be
        (real providers reject duplicate tool names with a 400)."""
        shared = f"twin-{uuid4().hex[:4]}"
        await create_skill(name=shared)
        await create_skill(name=shared)
        plan_call(no_confident_match=True)
        fake_llm.clear_seen_tools()
        fake_llm.push_ai("fallback handled it")
        fake_llm.push_ai("Aggregated: fallback handled it")
        run_id = await send_chat(client, "anything")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        from app.factory.worker import sanitize_tool_name

        base = f"use_skill_{sanitize_tool_name(shared)}"
        for names in fake_llm.seen_tools():
            assert len(names) == len(set(names)), f"duplicate bound tool names: {names}"
        flat = {n for names in fake_llm.seen_tools() for n in names}
        twins = [n for n in flat if n.startswith(base)]
        assert len(twins) == 2, f"both same-named skills must stay callable: {twins}"

    async def test_fallback_skill_isolation_holds(self, client: AsyncClient) -> None:
        bound = await create_tool(tool_name="orch_echo", tool_key=f"iso-{uuid4().hex[:4]}")
        await create_tool(tool_name="orch_echo", tool_key=f"noise-{uuid4().hex[:4]}")
        skill = await create_skill(name=f"iso-skill-{uuid4().hex[:4]}", tools=[bound])
        from app.factory.worker import sanitize_tool_name

        plan_call(no_confident_match=True)
        fake_llm.clear_seen_tools()
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": f"use_skill_{sanitize_tool_name(skill.name)}",
                    "args": {"task": "isolated work"},
                    "id": "s1",
                }
            ],
        )
        fake_llm.push_ai("skill inner result")  # inline skill loop
        fake_llm.push_ai("fallback final")  # fallback loop wraps up
        fake_llm.push_ai("Aggregated: fallback final")
        run_id = await send_chat(client, "needs the isolated skill")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        skill_calls = [
            names
            for names in fake_llm.seen_tools()
            if names == [sanitize_tool_name(bound.tool_key)]
        ]
        assert skill_calls, "the fallback-invoked skill loop must see only its bound tool"

    async def test_fallback_disabled_fails(self, client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(session, {"orchestrator_full_fallback_enabled": False})
        plan_call(no_confident_match=True)
        run_id = await send_chat(client, "nothing matches")
        run = await wait_run(client, run_id, {"failed"})
        assert "fallback is disabled" in run["error"]


class TestRecursionBudget:
    """GraphRecursionError must surface as a clean RunFailed chat message,
    never a raw traceback (seen live: a broken fetch tool kept the agentic
    loop retrying until the recursion limit)."""

    async def test_agentic_recursion_limit_fails_cleanly(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from langgraph.errors import GraphRecursionError

        import app.orchestrator.agentic_mode as agentic_mode

        class _RunawayAgent:
            async def astream(self, *args: Any, **kwargs: Any) -> Any:
                raise GraphRecursionError("Recursion limit of 100 reached")
                yield  # pragma: no cover - makes this an async generator

        async def fake_build() -> Any:
            return _RunawayAgent()

        monkeypatch.setattr(agentic_mode, "build_agentic_agent", fake_build)
        async with get_session_factory()() as session:
            await update_settings(session, {"orchestrator_mode": "agentic"})
        run_id = await send_chat(client, "loop forever")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "failed"
        assert "step budget" in (run["error"] or "")
        assert "GraphRecursionError" not in (run["error"] or "")
        assert "Traceback" not in (run["error"] or "")

    async def test_graph_recursion_limit_fails_cleanly(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from langgraph.errors import GraphRecursionError

        import app.orchestrator.runner as runner_module

        class _RunawayGraph:
            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
                raise GraphRecursionError("Recursion limit of 100 reached")

        monkeypatch.setattr(
            runner_module, "build_orchestrator_graph", lambda checkpointer: _RunawayGraph()
        )
        run_id = await send_chat(client, "graph loop forever")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "failed"
        assert "step budget" in (run["error"] or "")
        assert "GraphRecursionError" not in (run["error"] or "")


class TestCancelRetry:
    async def test_cancel_running(self, client: AsyncClient) -> None:
        tool = await create_tool(
            tool_name="orch_slow", tool_key=f"slow-{uuid4().hex[:4]}", direct_exposure=True
        )
        skill = await create_skill(
            name=f"slow-skill-{uuid4().hex[:4]}", tools=[tool], direct_exposure=True
        )
        from app.factory.worker import sanitize_tool_name

        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "slow work",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai(
            "",
            tool_calls=[{"name": sanitize_tool_name(tool.tool_key), "args": {}, "id": "sl1"}],
        )
        run_id = await send_chat(client, "start slow work")
        await asyncio.sleep(0.5)
        resp = await client.post(f"{API}/runs/{run_id}/cancel")
        assert resp.status_code == 200
        run = await wait_run(client, run_id, {"cancelled"})
        assert run["status"] == "cancelled"
        fake_llm.clear_script()

    async def test_retry_failed_run(self, client: AsyncClient) -> None:
        bogus = str(uuid4())
        for _ in range(2):
            plan_call(
                entries=[
                    {
                        "id": "s1",
                        "capability": {"type": "sub_agent", "id": bogus},
                        "task": "t",
                        "depends_on": [],
                    }
                ]
            )
        run_id = await send_chat(client)
        await wait_run(client, run_id, {"failed"})
        plan_call(direct_answer="second try works")
        resp = await client.post(f"{API}/runs/{run_id}/retry")
        assert resp.status_code == 201
        new_run = await wait_run(client, str(resp.json()["run_id"]), {"completed"})
        assert new_run["final_answer"] == "second try works"


class TestAgenticMode:
    @pytest.fixture(autouse=True)
    async def _agentic(self) -> None:
        async with get_session_factory()() as session:
            await update_settings(session, {"orchestrator_mode": "agentic"})

    async def test_simple_answer_and_labels(self, client: AsyncClient) -> None:
        fake_llm.push_ai("agentic direct answer")
        run_id = await send_chat(client, "hello agentic")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["orchestrator_mode"] == "agentic"
        assert run["final_answer"] == "agentic direct answer"
        assert steps_of_type(run, "aggregate")

    async def test_todo_plan_events_stream(self, client: AsyncClient) -> None:
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": {
                        "todos": [
                            {"content": "step one", "status": "in_progress"},
                            {"content": "step two", "status": "pending"},
                        ]
                    },
                    "id": "td1",
                }
            ],
        )
        fake_llm.push_ai("done with todos")
        run_id = await send_chat(client, "multi step request")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        from uuid import UUID as _UUID

        from app.orchestrator.context import EVENT_BUS

        history, queue = EVENT_BUS.subscribe(_UUID(run_id))
        EVENT_BUS.unsubscribe(_UUID(run_id), queue)
        plan_events = [e for e in history if e["type"] == "plan"]
        assert any(
            e["payload"].get("mode") == "agentic"
            and any(t["content"] == "step one" for t in e["payload"].get("todos", []))
            for e in plan_events
        )

    async def test_dispatch_tool_and_hitl(self, client: AsyncClient) -> None:
        from app.factory.worker import sanitize_tool_name

        s1 = await create_skill(name=f"ag-pre-{uuid4().hex[:4]}")
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
            },
            name=f"agentic-target-{uuid4().hex[:4]}",
        )
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": f"dispatch_{sanitize_tool_name(agent.name)}",
                    "args": {"task": "do the work"},
                    "id": "d1",
                }
            ],
        )
        fake_llm.push_ai("work node out")
        run_id = await send_chat(client, "use the sub agent")
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]

        fake_llm.push_ai("agentic final after approval")
        await client.post(f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": ""})
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["final_answer"] == "agentic final after approval"
        # resume rebuilds the agent (fresh middleware instances), so the
        # interrupted dispatch tool call must be replayed — the paused
        # dispatch step adopted and finished, the hitl decision recorded,
        # and nothing duplicated (spec §7.0 idempotent replay)
        dispatches = [
            s for s in steps_of_type(run, "skill") if s.get("node_id") == f"agentic:{agent.name}"
        ]
        assert len(dispatches) == 1, f"expected one dispatch step, got {len(dispatches)}"
        assert dispatches[0]["status"] == "completed"
        hitl_steps = steps_of_type(run, "hitl")
        assert len(hitl_steps) == 1 and hitl_steps[0]["status"] == "completed"
        work_steps = [s for s in steps_of_type(run, "skill") if s.get("node_id") == "work"]
        assert len(work_steps) == 1, "work node must run exactly once across pause/resume"
        assert route_rungs(run).count("custom_sub_agent") == 1, "route must not re-record on replay"

    async def test_double_hitl_resume_replay_alignment(self, client: AsyncClient) -> None:
        """Two consecutive hitl gates: each resume replays the dispatch tool
        from scratch, and stored resume values must stay index-aligned so each
        gate receives its own decision (spec §7.0 idempotent replay)."""
        from app.factory.worker import sanitize_tool_name

        agent = await create_sub_agent(
            {
                "nodes": [
                    {"id": "gate1", "type": "hitl", "prompt": "First?"},
                    {"id": "gate2", "type": "hitl", "prompt": "Second?"},
                ],
                "edges": [
                    {"from": "START", "to": "gate1"},
                    {"from": "gate1", "to": "gate2"},
                    {"from": "gate2", "to": "END"},
                ],
            },
            name=f"double-gate-{uuid4().hex[:4]}",
        )
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": f"dispatch_{sanitize_tool_name(agent.name)}",
                    "args": {"task": "run the gates"},
                    "id": "dg1",
                }
            ],
        )
        run_id = await send_chat(client, "double gate")
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]

        await client.post(
            f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": "first-note"}
        )
        # second pause: status alone is racy (still paused_hitl until the
        # resume task starts), so also require gate1's recorded hitl step
        deadline = asyncio.get_event_loop().time() + 20
        while True:
            run = (await client.get(f"{API}/runs/{run_id}")).json()
            gate1_done = any(s["node_id"] == "gate1" for s in steps_of_type(run, "hitl"))
            if run["status"] == "paused_hitl" and gate1_done:
                break
            assert run["status"] != "failed", run["error"]
            assert asyncio.get_event_loop().time() < deadline, "second pause never reached"
            await asyncio.sleep(0.1)

        fake_llm.push_ai("all gates passed")
        await client.post(
            f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": "second-note"}
        )
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["final_answer"] == "all gates passed"
        hitl_steps = {s["node_id"]: s for s in steps_of_type(run, "hitl")}
        assert set(hitl_steps) == {"gate1", "gate2"}
        assert "first-note" in str(hitl_steps["gate1"]["output"])
        assert "second-note" in str(hitl_steps["gate2"]["output"])

    async def test_mid_loop_tool_appearance(self, client: AsyncClient) -> None:
        """Middleware live-sync (spec §7.0): a tool ingested mid-loop is
        callable on the next model call, no rebuild."""
        from app.factory.worker import sanitize_tool_name

        inserter = await create_tool(
            tool_name="orch_inserter",
            tool_key=f"inserter-{uuid4().hex[:4]}",
            direct_exposure=True,
        )
        fake_llm.clear_seen_tools()
        fake_llm.push_ai(
            "",
            tool_calls=[{"name": sanitize_tool_name(inserter.tool_key), "args": {}, "id": "i1"}],
        )
        fake_llm.push_ai("saw the new tool")
        run_id = await send_chat(client, "insert mid-loop")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        seen = fake_llm.seen_tools()
        assert not any(n.startswith("midloop-") for n in seen[0])
        assert any(n.startswith("midloop-") for n in seen[-1]), (
            "second model call must see the tool ingested between calls"
        )

    async def test_use_full_catalog_escalation(self, client: AsyncClient) -> None:
        unexposed = await create_tool(
            tool_name="orch_echo", tool_key=f"esc-{uuid4().hex[:4]}", direct_exposure=False
        )
        from app.factory.worker import sanitize_tool_name

        fake_llm.clear_seen_tools()
        fake_llm.push_ai("", tool_calls=[{"name": "use_full_catalog", "args": {}, "id": "fc1"}])
        fake_llm.push_ai("escalated and done")
        run_id = await send_chat(client, "need more tools")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "fallback" in route_rungs(run)
        seen = fake_llm.seen_tools()
        key = sanitize_tool_name(unexposed.tool_key)
        assert key not in seen[0]
        assert key in seen[-1]


class TestAnswerUi:
    async def test_answer_ui_generated_as_a2ui(self, client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(session, {"formatter_enabled": True})
        plan_call(direct_answer="Revenue was down 12% in FY2024.")
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": "AnswerUi",
                    "args": {
                        "components": [
                            {"type": "stat", "label": "Revenue change", "value": "-12%"},
                            {"type": "text", "markdown": "Driven by churn in Q3."},
                        ]
                    },
                    "id": "ui1",
                }
            ],
        )
        run_id = await send_chat(client, "revenue?")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        messages = run["answer_ui"]["a2ui"]
        assert messages[0]["createSurface"]["catalogId"].startswith("https://a2ui.org/")
        components = messages[1]["updateComponents"]["components"]
        assert any(c["component"] == "Text" and "-12%" in str(c.get("text")) for c in components)
        assert any(c["id"] == "root" for c in components)

    async def test_answer_ui_failure_safe(self, client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(session, {"formatter_enabled": True})
        plan_call(direct_answer="Plain answer.")
        fake_llm.push_ai("not a structured payload at all")
        run_id = await send_chat(client, "plain?")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed"
        assert run["answer_ui"] is None


class TestSseAndMetrics:
    async def test_sse_stream_replays_contract_events(self, client: AsyncClient) -> None:
        plan_call(direct_answer="streamed answer")
        run_id = await send_chat(client, "stream me")
        await wait_run(client, run_id, {"completed"})
        events: list[str] = []
        async with client.stream("GET", f"{API}/chat/stream/{run_id}") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
                if "done" in events:
                    break
        assert "run_status" in events
        assert "plan" in events
        assert "token" in events
        assert events[-1] == "done"

    async def test_metrics_exposed(self, client: AsyncClient) -> None:
        plan_call(direct_answer="metric answer")
        run_id = await send_chat(client, "metrics")
        await wait_run(client, run_id, {"completed"})
        body = (await client.get("/metrics")).text
        assert "concierge_runs_total" in body
        assert "concierge_steps_total" in body


async def _count_steps(run_id: str) -> int:
    async with get_session_factory()() as session:
        from uuid import UUID as _UUID

        return len(
            list(
                (
                    await session.execute(select(RunStep).where(RunStep.run_id == _UUID(run_id)))
                ).scalars()
            )
        )


class TestRunHousekeeping:
    async def test_delete_and_purge(self, client: AsyncClient) -> None:
        plan_call(direct_answer="a")
        run_id = await send_chat(client, "one")
        await wait_run(client, run_id, {"completed"})
        assert (await client.delete(f"{API}/runs/{run_id}")).status_code == 204
        assert (await client.get(f"{API}/runs/{run_id}")).status_code == 404

        plan_call(direct_answer="b")
        run_id2 = await send_chat(client, "two")
        await wait_run(client, run_id2, {"completed"})
        assert (await client.delete(f"{API}/runs")).status_code == 204
        assert (await client.get(f"{API}/runs")).json() == []

    async def test_run_and_steps_recorded(self, client: AsyncClient) -> None:
        plan_call(direct_answer="recorded")
        run_id = await send_chat(client, "record me")
        run = await wait_run(client, run_id, {"completed"})
        assert await _count_steps(run_id) >= 1
        assert run["total_input_tokens"] > 0

    async def test_delete_running_run_conflict(self, client: AsyncClient) -> None:
        tool = await create_tool(
            tool_name="orch_slow", tool_key=f"slow2-{uuid4().hex[:4]}", direct_exposure=True
        )
        skill = await create_skill(
            name=f"slow2-{uuid4().hex[:4]}", tools=[tool], direct_exposure=True
        )
        from app.factory.worker import sanitize_tool_name

        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "slow",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai(
            "", tool_calls=[{"name": sanitize_tool_name(tool.tool_key), "args": {}, "id": "s"}]
        )
        run_id = await send_chat(client, "slow again")
        await asyncio.sleep(0.3)
        assert (await client.delete(f"{API}/runs/{run_id}")).status_code == 409
        await client.post(f"{API}/runs/{run_id}/cancel")
        await wait_run(client, run_id, {"cancelled"})
        fake_llm.clear_script()


class TestBlockContent:
    """Reasoning-enabled models return list-of-blocks message content —
    prose extraction must keep text blocks only, never the block repr."""

    async def test_agentic_final_answer_extracts_text_blocks(self, client: AsyncClient) -> None:
        from langchain_core.messages import AIMessage

        async with get_session_factory()() as session:
            await update_settings(session, {"orchestrator_mode": "agentic"})
        fake_llm.push_message(
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "let me think", "signature": "sig123"},
                    {"type": "text", "text": "clean agentic answer"},
                ]
            )
        )
        run_id = await send_chat(client, "block content agentic")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["final_answer"] == "clean agentic answer"

    async def test_graph_skill_and_aggregate_extract_text_blocks(self, client: AsyncClient) -> None:
        from langchain_core.messages import AIMessage

        skill = await create_skill(name=f"blocks-{uuid4().hex[:4]}", direct_exposure=True)
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "produce prose",
                    "depends_on": [],
                }
            ]
        )
        blocks = lambda text: [  # noqa: E731
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "text", "text": text},
        ]
        fake_llm.push_message(AIMessage(content=blocks("skill prose")))  # inline skill loop
        fake_llm.push_message(AIMessage(content=blocks("aggregated prose")))  # aggregator
        run_id = await send_chat(client, "block content graph")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["final_answer"] == "aggregated prose"
        skill_steps = [s for s in steps_of_type(run, "skill") if s.get("output")]
        assert any("skill prose" in str(s["output"]) for s in skill_steps)
        assert not any("signature" in str(s["output"]) for s in skill_steps)


class TestAggregatorEmptyAnswer:
    """Thinking models occasionally stream an empty completion — the
    aggregator retries once, then fails the run instead of persisting a
    completed run with an empty final answer."""

    def _plan_one_skill(self, skill_id: str) -> None:
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": skill_id},
                    "task": "produce prose",
                    "depends_on": [],
                }
            ]
        )

    async def test_empty_aggregation_retried_once(self, client: AsyncClient) -> None:
        from langchain_core.messages import AIMessage

        skill = await create_skill(name=f"aggretry-{uuid4().hex[:4]}", direct_exposure=True)
        self._plan_one_skill(str(skill.id))
        fake_llm.push_ai("skill prose")  # inline skill loop
        fake_llm.push_message(AIMessage(content=""))  # aggregator: empty completion
        fake_llm.push_ai("recovered aggregate")  # aggregator retry
        run_id = await send_chat(client, "empty aggregate once")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["final_answer"] == "recovered aggregate"

    async def test_empty_aggregation_twice_fails_run(self, client: AsyncClient) -> None:
        from langchain_core.messages import AIMessage

        skill = await create_skill(name=f"aggfail-{uuid4().hex[:4]}", direct_exposure=True)
        self._plan_one_skill(str(skill.id))
        fake_llm.push_ai("skill prose")
        fake_llm.push_message(AIMessage(content=""))
        fake_llm.push_message(AIMessage(content=""))
        run_id = await send_chat(client, "empty aggregate twice")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "failed"
        assert "empty answer" in (run["error"] or "")


class TestParallelHitl:
    async def test_two_parallel_gates_approved_one_at_a_time(self, client: AsyncClient) -> None:
        """Parallel dispatch can leave two interrupts pending at once; each
        POST /hitl answers exactly one gate and the run pauses again until
        every gate is resolved (spec §7.1 parallel dispatch + §7 HITL)."""
        sa = await create_skill(name=f"pa-{uuid4().hex[:4]}")
        sb = await create_skill(name=f"pb-{uuid4().hex[:4]}")
        edges = [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "gate"},
            {"from": "gate", "to": "END"},
        ]
        agent_a = await create_sub_agent(
            {
                "nodes": [
                    {"id": "work", "type": "skill", "skill_id": str(sa.id)},
                    {"id": "gate", "type": "hitl", "prompt": "Gate A?"},
                ],
                "edges": edges,
            },
            name=f"par-a-{uuid4().hex[:4]}",
        )
        agent_b = await create_sub_agent(
            {
                "nodes": [
                    {"id": "work", "type": "skill", "skill_id": str(sb.id)},
                    {"id": "gate", "type": "hitl", "prompt": "Gate B?"},
                ],
                "edges": edges,
            },
            name=f"par-b-{uuid4().hex[:4]}",
        )
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "sub_agent", "id": str(agent_a.id)},
                    "task": "task a",
                    "depends_on": [],
                },
                {
                    "id": "s2",
                    "capability": {"type": "sub_agent", "id": str(agent_b.id)},
                    "task": "task b",
                    "depends_on": [],
                },
            ]
        )
        fake_llm.push_ai("work output one")
        fake_llm.push_ai("work output two")
        run_id = await send_chat(client, "run both gated branches")
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]

        # first approval resolves ONE gate; the run must pause again
        await client.post(f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": "1"})
        deadline = asyncio.get_event_loop().time() + 20
        while True:
            run = (await client.get(f"{API}/runs/{run_id}")).json()
            if run["status"] == "paused_hitl" and len(steps_of_type(run, "hitl")) == 1:
                break
            assert run["status"] != "failed", run["error"]
            assert run["status"] != "completed", "one approval must not complete two gates"
            assert asyncio.get_event_loop().time() < deadline, "second pause never reached"
            await asyncio.sleep(0.1)

        fake_llm.push_ai("Aggregated: both branches done")
        await client.post(f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": "2"})
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert len(steps_of_type(run, "hitl")) == 2
        dispatches = [s for s in steps_of_type(run, "skill") if s.get("node_id") in ("s1", "s2")]
        assert sorted(s["node_id"] for s in dispatches) == ["s1", "s2"]
        assert all(s["status"] == "completed" for s in dispatches)
        work_steps = [s for s in steps_of_type(run, "skill") if s.get("node_id") == "work"]
        assert len(work_steps) == 2, "each worker's work node runs exactly once"


class TestLiveEvents:
    async def test_activity_and_thinking_events_stream(self, client: AsyncClient) -> None:
        """spec §7.1: every step transition emits `activity` (the chat's live
        ticker), and streamed reasoning emits `thinking` — separate from
        `token` prose."""
        from uuid import UUID as _UUID

        from langchain_core.messages import AIMessage

        from app.orchestrator.context import EVENT_BUS

        skill = await create_skill(name=f"act-{uuid4().hex[:4]}", direct_exposure=True)
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "do it",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("skill out")
        fake_llm.push_message(
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "pondering the merge", "signature": "s"},
                    {"type": "text", "text": "final merged answer"},
                ]
            )
        )
        run_id = await send_chat(client, "activity stream test")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        history, queue = EVENT_BUS.subscribe(_UUID(run_id))
        EVENT_BUS.unsubscribe(_UUID(run_id), queue)
        acts = [e for e in history if e["type"] == "activity"]
        assert any(
            e["payload"].get("step_type") == "skill" and e["payload"].get("entity_name")
            for e in acts
        ), "running steps must announce themselves with an entity name"
        assert any(e["payload"].get("status") == "completed" for e in acts)
        thinks = [e for e in history if e["type"] == "thinking"]
        assert any("pondering" in str(e["payload"].get("text")) for e in thinks)
        assert run["final_answer"] == "final merged answer"


class TestSpinWorkerStrictIds:
    """spin_worker gets its skill_ids from the agentic MODEL. The contract is
    strict — registry uuids only, never names — and violations must surface
    as corrective tool feedback (ResolutionError → message), never as a raw
    ValueError that kills the run. The model can comply because the injected
    skills catalog carries each skill's registry id."""

    async def test_dynamic_resolution_rejects_names_with_guidance(self) -> None:
        from app.orchestrator.ladder import ResolutionError, resolve_capability

        skill = await create_skill(name=f"byname-{uuid4().hex[:6]}")
        with pytest.raises(ResolutionError) as exc:
            await resolve_capability({"type": "spin_worker", "skill_ids": [skill.name]})
        assert "uuid" in str(exc.value)
        assert "use_full_catalog" in str(exc.value)

    async def test_dynamic_resolution_rejects_garbage_as_resolution_error(self) -> None:
        from app.orchestrator.ladder import ResolutionError, resolve_capability

        with pytest.raises(ResolutionError):
            await resolve_capability({"type": "spin_worker", "skill_ids": ["no-such-skill"]})

    async def test_dynamic_resolution_accepts_valid_uuid(self) -> None:
        from app.orchestrator.ladder import resolve_capability

        skill = await create_skill(name=f"byid-{uuid4().hex[:6]}", direct_exposure=True)
        res = await resolve_capability({"type": "spin_worker", "skill_ids": [str(skill.id)]})
        assert res.rung == "dynamic_worker"
        assert res.payload["skills"][0]["id"] == str(skill.id)

    async def test_dynamic_resolution_rejects_unknown_uuid(self) -> None:
        from app.orchestrator.ladder import ResolutionError, resolve_capability

        with pytest.raises(ResolutionError):
            await resolve_capability({"type": "spin_worker", "skill_ids": [str(uuid4())]})

    async def test_spin_worker_tool_degrades_to_error_message(self) -> None:
        from app.orchestrator.agentic_mode import _spin_worker_tool
        from app.orchestrator.context import RunContext, set_run_context
        from app.orchestrator.recorder import RunRecorder

        run_id = uuid4()
        set_run_context(
            RunContext(
                run_id=run_id,
                mode="agentic",
                recorder=RunRecorder(run_id),
                settings={},
                callbacks=[],
            )
        )
        tool = _spin_worker_tool()
        out = await tool.coroutine(skill_ids=["not-a-skill"], task="anything")
        assert "could not spin a worker" in out
        assert "uuid" in out

    async def test_skills_catalog_prompt_lines_carry_registry_ids(self) -> None:
        from app.orchestrator.middleware import SkillsRegistryMiddleware

        skill = await create_skill(name=f"catalog-{uuid4().hex[:6]}", direct_exposure=True)
        mw = SkillsRegistryMiddleware(mode="exposed")

        class _Req:
            tools: list[Any] = []
            system_message = None

            def override(self, **kw: Any) -> "_Req":
                req = _Req()
                for key, value in kw.items():
                    setattr(req, key, value)
                return req

        seen: dict[str, Any] = {}

        async def handler(req: Any) -> str:
            seen["system"] = req.system_message.text if req.system_message is not None else ""
            return "ok"

        await mw.awrap_model_call(_Req(), handler)
        assert f"(skill id: {skill.id})" in seen["system"]


class TestEphemeralWorkerExposureGate:
    """spec §7.1 rung 4: an ephemeral worker composes EXPOSED skills only.
    A hidden skill is reachable through a sub agent that owns it, or through
    the full-catalog fallback which runs it inline in the open — never by
    laundering its id into a worker the user was never shown."""

    @staticmethod
    def _capture_req() -> Any:
        class _Req:
            tools: list[Any] = []
            system_message = None

            def override(self, **kw: Any) -> Any:
                req = _Req()
                for key, value in kw.items():
                    setattr(req, key, value)
                return req

        return _Req()

    async def test_spin_worker_rejects_non_exposed_skill(self) -> None:
        from app.orchestrator.ladder import ResolutionError, resolve_capability

        skill = await create_skill(name=f"hidden-{uuid4().hex[:6]}")
        assert skill.direct_exposure is False
        with pytest.raises(ResolutionError) as exc:
            await resolve_capability({"type": "spin_worker", "skill_ids": [str(skill.id)]})
        assert "not exposed" in str(exc.value)
        assert "use_full_catalog" in str(exc.value)

    async def test_spin_worker_rejects_a_mixed_batch(self) -> None:
        """One hidden skill poisons the whole composition — no partial worker."""
        from app.orchestrator.ladder import ResolutionError, resolve_capability

        ok = await create_skill(name=f"open-{uuid4().hex[:6]}", direct_exposure=True)
        hidden = await create_skill(name=f"shut-{uuid4().hex[:6]}")
        with pytest.raises(ResolutionError):
            await resolve_capability(
                {"type": "spin_worker", "skill_ids": [str(ok.id), str(hidden.id)]}
            )

    async def test_direct_skill_descent_never_spins_a_worker_for_hidden_skill(self) -> None:
        """A hidden skill nobody covers used to fall through to rung 4. It now
        stops with guidance instead of manufacturing a worker over it."""
        from app.orchestrator.ladder import ResolutionError, resolve_capability

        skill = await create_skill(name=f"uncovered-{uuid4().hex[:6]}")
        with pytest.raises(ResolutionError) as exc:
            await resolve_capability({"type": "direct_skill", "id": str(skill.id)})
        assert "not exposed" in str(exc.value)

    async def test_hidden_skill_still_reachable_through_its_sub_agent(self) -> None:
        """The gate closes rung 4 only — rungs 2-3 remain the sanctioned route."""
        from app.orchestrator.ladder import resolve_capability

        skill = await create_skill(name=f"owned-{uuid4().hex[:6]}")
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "n", "type": "skill", "skill_id": str(skill.id)}],
                "edges": [{"from": "START", "to": "n"}, {"from": "n", "to": "END"}],
            },
            name=f"owner-{uuid4().hex[:4]}",
        )
        res = await resolve_capability({"type": "direct_skill", "id": str(skill.id)})
        assert res.rung == "custom_sub_agent"
        assert res.entity_id == str(agent.id)

    async def test_fallback_learned_id_cannot_be_laundered_into_a_worker(self) -> None:
        """The escalation that reveals hidden ids does not widen what a worker
        may compose: full_catalog is a read surface, not a grant."""
        from app.orchestrator.context import RunContext, RunFlags, set_run_context
        from app.orchestrator.ladder import ResolutionError, resolve_capability
        from app.orchestrator.recorder import RunRecorder

        run_id = uuid4()
        set_run_context(
            RunContext(
                run_id=run_id,
                mode="agentic",
                recorder=RunRecorder(run_id),
                settings={},
                callbacks=[],
                flags=RunFlags(full_catalog=True),
            )
        )
        skill = await create_skill(name=f"laundered-{uuid4().hex[:6]}")
        with pytest.raises(ResolutionError) as exc:
            await resolve_capability({"type": "spin_worker", "skill_ids": [str(skill.id)]})
        assert "not exposed" in str(exc.value)

    async def test_full_catalog_line_withholds_the_id_of_a_hidden_skill(self) -> None:
        """spin_worker's contract is ids-only, so the catalog simply does not
        print an id the model is not allowed to compose (spec §7.1 rung 4)."""
        from app.orchestrator.middleware import SkillsRegistryMiddleware

        shown = await create_skill(name=f"shown-{uuid4().hex[:6]}", direct_exposure=True)
        hidden = await create_skill(name=f"veiled-{uuid4().hex[:6]}")
        mw = SkillsRegistryMiddleware(mode="full_catalog")
        seen: dict[str, Any] = {}

        async def handler(req: Any) -> str:
            seen["system"] = req.system_message.text if req.system_message is not None else ""
            return "ok"

        await mw.awrap_model_call(self._capture_req(), handler)
        system = seen["system"]
        assert f"(skill id: {shown.id})" in system
        assert str(hidden.id) not in system
        assert hidden.name in system  # still callable inline — just not composable
        assert "fallback only" in system


class TestToolFailureContainment:
    """spec §5: a dead MCP server (or any exception inside a tool) surfaces
    as a TOOL error. Agentic/fallback loops receive an error ToolMessage and
    keep going; strict skill loops keep node error-edge semantics."""

    @staticmethod
    def _mw_with_raising_tool(strict: bool) -> Any:
        from langchain_core.tools import StructuredTool

        from app.orchestrator.middleware import ToolsRegistryMiddleware

        async def boom() -> str:
            raise RuntimeError("MCP server dead-beef is not connected")

        tool = StructuredTool.from_function(coroutine=boom, name="boom_tool", description="boom")
        mw = ToolsRegistryMiddleware(mode="exposed", strict_tool_errors=strict)
        mw._current = {"boom_tool": tool}
        mw._meta = {"boom_tool": {"kind": "mcp", "source": "dynamic", "id": str(uuid4())}}
        return mw, tool

    @staticmethod
    def _request() -> Any:
        class _Req:
            tool_call = {"name": "boom_tool", "id": "call_1", "args": {}}

            def override(self, **kw: Any) -> "_Req":
                req = _Req()
                for key, value in kw.items():
                    setattr(req, key, value)
                return req

        return _Req()

    async def test_agentic_mode_contains_tool_exception(self) -> None:
        from langchain_core.messages import ToolMessage

        from app.orchestrator.context import RunContext, set_run_context
        from app.orchestrator.recorder import RunRecorder
        from app.orchestrator.runner import create_run

        run = await create_run(None, "containment test agentic")
        run_id = run.id
        set_run_context(
            RunContext(
                run_id=run_id,
                mode="agentic",
                recorder=RunRecorder(run_id),
                settings={},
                callbacks=[],
            )
        )
        mw, tool = self._mw_with_raising_tool(strict=False)

        async def handler(req: Any) -> Any:
            return await req.tool.ainvoke(req.tool_call["args"])

        result = await mw.awrap_tool_call(self._request(), handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "is not connected" in str(result.content)

    async def test_strict_mode_keeps_error_edge_semantics(self) -> None:
        from app.orchestrator.context import RunContext, set_run_context
        from app.orchestrator.middleware import ToolExecutionFailed
        from app.orchestrator.recorder import RunRecorder
        from app.orchestrator.runner import create_run

        run = await create_run(None, "containment test strict")
        run_id = run.id
        set_run_context(
            RunContext(
                run_id=run_id,
                mode="graph",
                recorder=RunRecorder(run_id),
                settings={},
                callbacks=[],
            )
        )
        mw, tool = self._mw_with_raising_tool(strict=True)

        async def handler(req: Any) -> Any:
            return await req.tool.ainvoke(req.tool_call["args"])

        with pytest.raises(ToolExecutionFailed):
            await mw.awrap_tool_call(self._request(), handler)


class TestChatPresentationContracts:
    """dispatch events carry the entity name (spec §7.1) and reloaded
    conversations keep user→response interleaving for failed runs."""

    async def test_dispatch_events_carry_entity_name(self, client: AsyncClient) -> None:
        from uuid import UUID as _U

        from app.orchestrator.context import EVENT_BUS

        skill = await create_skill(name=f"named-disp-{uuid4().hex[:4]}", direct_exposure=True)
        plan_call(
            entries=[
                {
                    "id": "s1",
                    "capability": {"type": "direct_skill", "id": str(skill.id)},
                    "task": "do it",
                    "depends_on": [],
                }
            ]
        )
        fake_llm.push_ai("skill output")
        fake_llm.push_ai("Aggregated.")
        run_id = await send_chat(client, "named dispatch test")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        history, queue = EVENT_BUS.subscribe(_U(run_id))
        EVENT_BUS.unsubscribe(_U(run_id), queue)
        starts = [e for e in history if e["type"] == "dispatch_start"]
        ends = [e for e in history if e["type"] == "dispatch_end"]
        assert starts and ends
        assert any(e["payload"].get("entity_name") == skill.name for e in starts)
        assert any(e["payload"].get("entity_name") == skill.name for e in ends)

    async def test_failed_run_error_appears_in_conversation(self, client: AsyncClient) -> None:
        fake_llm.push_error(RuntimeError("planner exploded for the test"))
        fake_llm.push_error(RuntimeError("planner exploded for the test"))
        run_id = await send_chat(client, "history error test")
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "failed"
        conv = (await client.get(f"{API}/conversations/{run['conversation_id']}")).json()
        roles = [m["role"] for m in conv["messages"]]
        assert roles == ["user", "error"]
        assert "planner exploded" in conv["messages"][1]["content"]
        assert conv["messages"][1]["run_id"] == run_id


class TestLineageAndCallsigns:
    """Chat grouping lineage (spec §7.1/§8.5): hitl_request carries the owning
    dispatch step, activity carries parent_step_id; ephemeral workers get
    per-run phonetic callsigns with their skill composition."""

    async def test_hitl_request_carries_dispatch_step_and_activity_parents(
        self, client: AsyncClient
    ) -> None:
        from uuid import UUID as _U

        from app.factory.worker import sanitize_tool_name
        from app.orchestrator.context import EVENT_BUS
        from app.settings_store import update_settings

        async with get_session_factory()() as session:
            await update_settings(session, {"orchestrator_mode": "agentic"})
        s1 = await create_skill(name=f"lin-{uuid4().hex[:4]}")
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
            },
            name=f"lineage-{uuid4().hex[:4]}",
        )
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": f"dispatch_{sanitize_tool_name(agent.name)}",
                    "args": {"task": "work"},
                    "id": "d1",
                }
            ],
        )
        fake_llm.push_ai("work out")
        run_id = await send_chat(client, "lineage test")
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]
        history, queue = EVENT_BUS.subscribe(_U(run_id))
        EVENT_BUS.unsubscribe(_U(run_id), queue)
        dispatch = next(e for e in history if e["type"] == "dispatch_start")
        gate = next(e for e in history if e["type"] == "hitl_request")
        assert gate["payload"]["step_id"] == dispatch["payload"]["step_id"]
        acts = [e for e in history if e["type"] == "activity" and e["payload"].get("step_type")]
        assert any(
            a["payload"].get("parent_step_id") == dispatch["payload"]["step_id"] for a in acts
        ), "worker-internal steps must carry the dispatch step as parent"
        await client.post(f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": ""})
        fake_llm.push_ai("done")
        await wait_run(client, run_id, {"completed", "failed"})

    async def test_worker_callsigns_sequence_and_composition(self) -> None:
        from app.orchestrator.context import RunContext, set_run_context
        from app.orchestrator.ladder import resolve_capability
        from app.orchestrator.recorder import RunRecorder
        from app.orchestrator.runner import create_run

        run = await create_run(None, "callsign test")
        set_run_context(
            RunContext(
                run_id=run.id,
                mode="agentic",
                recorder=RunRecorder(run.id),
                settings={},
                callbacks=[],
            )
        )
        s1 = await create_skill(name=f"cs-one-{uuid4().hex[:4]}", direct_exposure=True)
        s2 = await create_skill(name=f"cs-two-{uuid4().hex[:4]}", direct_exposure=True)
        r1 = await resolve_capability(
            {"type": "spin_worker", "skill_ids": [str(s1.id), str(s2.id)]}
        )
        r2 = await resolve_capability({"type": "spin_worker", "skill_ids": [str(s2.id)]})
        assert r1.entity_name == f"worker-alpha ({s1.name}+{s2.name})"
        assert r2.entity_name == f"worker-bravo ({s2.name})"
        assert r1.payload["callsign"] == "worker-alpha"
        assert r2.payload["callsign"] == "worker-bravo"
