"""Direct sub-agent invocation (spec §7.5): surfaces, gating, lifecycle.

The pin replaces the routing decision, never the run lifecycle — so these
tests assert the full lifecycle on direct runs: route/skill steps, HITL
pause/resume on the shared checkpointer, retry preserving the pin, and the
same terminal statuses as routed runs."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import SubAgent
from app.native.provider import native_sub_agent
from app.settings_store import update_settings
from tests.factory_helpers import create_skill, create_sub_agent

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _direct_settings() -> None:
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "default_model": "fake:scripted",
                "formatter_enabled": False,
                "orchestrator_mode": "graph",
            },
        )


def single_node_workflow(skill_id: str) -> dict[str, Any]:
    return {
        "nodes": [{"id": "work", "type": "skill", "skill_id": skill_id}],
        "edges": [{"from": "START", "to": "work"}, {"from": "work", "to": "END"}],
    }


async def make_exposed_agent(**kw: Any) -> SubAgent:
    skill = await create_skill(name=f"direct-{uuid4().hex[:4]}")
    return await create_sub_agent(
        single_node_workflow(str(skill.id)),
        name=kw.pop("name", f"direct-agent-{uuid4().hex[:4]}"),
        direct_exposure=kw.pop("direct_exposure", True),
        **kw,
    )


async def wait_run(
    client: AsyncClient, run_id: str, statuses: set[str], timeout_s: float = 20.0
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    run: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        run = (await client.get(f"{API}/runs/{run_id}")).json()
        if run["status"] in statuses:
            return dict(run)
        await asyncio.sleep(0.1)
    raise AssertionError(f"run did not reach {statuses}; last: {run.get('status')}")


def steps_of_type(run: dict[str, Any], step_type: str) -> list[dict[str, Any]]:
    return [s for s in run["steps"] if s["step_type"] == step_type]


class TestInvokeEndpointGating:
    async def test_unknown_agent_404(self, client: AsyncClient) -> None:
        resp = await client.post(f"{API}/sub-agents/{uuid4()}/invoke", json={"message": "hi"})
        assert resp.status_code == 404

    async def test_not_exposed_403(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent(direct_exposure=False)
        resp = await client.post(f"{API}/sub-agents/{agent.id}/invoke", json={"message": "hi"})
        assert resp.status_code == 403
        assert "not exposed" in resp.json()["detail"]

    async def test_inactive_409(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent(status="inactive")
        resp = await client.post(f"{API}/sub-agents/{agent.id}/invoke", json={"message": "hi"})
        assert resp.status_code == 409

    async def test_empty_message_422(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent()
        resp = await client.post(f"{API}/sub-agents/{agent.id}/invoke", json={"message": "  "})
        assert resp.status_code == 422

    async def test_chat_target_gating_matches(self, client: AsyncClient) -> None:
        hidden = await make_exposed_agent(direct_exposure=False)
        resp = await client.post(
            f"{API}/chat", json={"message": "hi", "target_sub_agent_id": str(hidden.id)}
        )
        assert resp.status_code == 403
        resp = await client.post(
            f"{API}/chat", json={"message": "hi", "target_sub_agent_id": str(uuid4())}
        )
        assert resp.status_code == 404


class TestDirectExecution:
    async def test_invoke_endpoint_runs_custom_agent(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent()
        fake_llm.push_ai("direct worker output")
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke", json={"message": "do it directly"}
        )
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["run_id"]
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["orchestrator_mode"] == "direct"
        assert run["target_sub_agent_id"] == str(agent.id)
        assert "direct worker output" in run["final_answer"]
        # trace parity (spec §7.5): one pinned ladder route with the sub-agent
        # rung (worker-internal route: nodes may add more), no plan step
        ladder_routes = [
            s for s in steps_of_type(run, "route") if (s.get("output") or {}).get("rung")
        ]
        assert len(ladder_routes) == 1
        assert ladder_routes[0]["output"]["rung"] == "custom_sub_agent"
        assert ladder_routes[0]["output"]["resolved_to"]["entity_name"] == agent.name
        assert steps_of_type(run, "plan") == []

    async def test_chat_target_pins_run_into_conversation(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent()
        conv = (await client.post(f"{API}/conversations", json={"title": "pinned"})).json()
        fake_llm.push_ai("pinned answer")
        resp = await client.post(
            f"{API}/chat",
            json={
                "message": "handle this",
                "conversation_id": conv["id"],
                "target_sub_agent_id": str(agent.id),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["conversation_id"] == conv["id"]
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["orchestrator_mode"] == "direct"
        # the conversation surfaces the direct run like any other
        detail = (await client.get(f"{API}/conversations/{conv['id']}")).json()
        assert any(r["target_sub_agent_id"] == str(agent.id) for r in detail["runs"])

    async def test_chat_without_target_unchanged(self, client: AsyncClient) -> None:
        """No target → plain orchestrator run, byte-for-byte legacy behavior."""
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": "PlannerOutput",
                    "args": {
                        "entries": [],
                        "direct_answer": "plain answer",
                        "no_confident_match": False,
                    },
                    "id": f"p{uuid4().hex[:6]}",
                }
            ],
        )
        resp = await client.post(f"{API}/chat", json={"message": "hello"})
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["orchestrator_mode"] == "graph"
        assert run["target_sub_agent_id"] is None

    async def test_native_agent_direct_invoke(self, client: AsyncClient) -> None:
        agent_name = f"native-direct-{uuid4().hex[:4]}"

        @native_sub_agent(agent_name, "native direct target", covers_skill_ids=[])
        def build_native(checkpointer: Any) -> Any:
            from langgraph.graph import END, START, StateGraph

            from app.factory.worker import WorkerState

            async def work(state: WorkerState) -> dict[str, Any]:
                return {"node_outputs": {"nd": {"status": "ok", "output": "native direct output"}}}

            g = StateGraph(WorkerState)
            g.add_node("nd", work)
            g.add_edge(START, "nd")
            g.add_edge("nd", END)
            return g.compile(checkpointer=checkpointer)

        from app.seed.loader import upsert_native_sub_agents

        async with get_session_factory()() as session:
            await upsert_native_sub_agents(session)
        from app.registry_cache import get_cache

        await get_cache().invalidate("sub_agents")
        from sqlalchemy import select

        async with get_session_factory()() as session:
            agent = (
                await session.execute(select(SubAgent).where(SubAgent.name == agent_name))
            ).scalar_one()
            agent_id = agent.id
            assert agent.direct_exposure is True  # static seeds ship exposed

        resp = await client.post(f"{API}/sub-agents/{agent_id}/invoke", json={"message": "go"})
        assert resp.status_code == 201, resp.text
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "native direct output" in run["final_answer"]
        routes = steps_of_type(run, "route")
        assert routes and routes[0]["output"]["rung"] == "native_sub_agent"


class TestDirectHitl:
    async def make_hitl_agent(self) -> SubAgent:
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
            name=f"direct-hitl-{uuid4().hex[:4]}",
            direct_exposure=True,
        )

    async def test_pause_approve_resume_no_duplicates(self, client: AsyncClient) -> None:
        agent = await self.make_hitl_agent()
        fake_llm.push_ai("work output")
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke", json={"message": "work then save"}
        )
        run_id = resp.json()["run_id"]
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]
        pending = (await client.get(f"{API}/hitl/pending")).json()
        assert any(p["run_id"] == run_id for p in pending)

        fake_llm.push_ai("save output")
        resp = await client.post(
            f"{API}/runs/{run_id}/hitl", json={"decision": "approve", "note": "go"}
        )
        assert resp.status_code == 200
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["orchestrator_mode"] == "direct"
        assert len(steps_of_type(run, "hitl")) == 1
        # resume replays never duplicate the pre-graph route step or dispatch
        ladder_routes = [
            s for s in steps_of_type(run, "route") if (s.get("output") or {}).get("rung")
        ]
        assert len(ladder_routes) == 1
        work_steps = [s for s in steps_of_type(run, "skill") if s.get("node_id") == "work"]
        assert len(work_steps) == 1

    async def test_deny_completes_without_save(self, client: AsyncClient) -> None:
        agent = await self.make_hitl_agent()
        fake_llm.push_ai("work output")
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke", json={"message": "work then save"}
        )
        run_id = resp.json()["run_id"]
        run = await wait_run(client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]
        resp = await client.post(
            f"{API}/runs/{run_id}/hitl", json={"decision": "deny", "note": "not now"}
        )
        assert resp.status_code == 200
        run = await wait_run(client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        save_steps = [s for s in steps_of_type(run, "skill") if s.get("node_id") == "save"]
        assert save_steps == []


class TestDirectLifecycle:
    async def test_failure_then_retry_preserves_pin(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent()
        fake_llm.push_error(RuntimeError("model exploded"))
        resp = await client.post(f"{API}/sub-agents/{agent.id}/invoke", json={"message": "boom"})
        run_id = resp.json()["run_id"]
        run = await wait_run(client, run_id, {"failed", "completed"})
        assert run["status"] == "failed"
        assert "direct invocation" in run["error"]

        fake_llm.push_ai("second time works")
        resp = await client.post(f"{API}/runs/{run_id}/retry")
        assert resp.status_code == 201, resp.text
        new_run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert new_run["status"] == "completed", new_run["error"]
        assert new_run["orchestrator_mode"] == "direct"
        assert new_run["target_sub_agent_id"] == str(agent.id)

    async def test_exposure_flip_between_create_and_execute_fails_clean(
        self, client: AsyncClient
    ) -> None:
        """Defense in depth: the execution-start gate catches a toggle flipped
        after the API check (simulated by patching the flag off and re-running
        the same target through create_run directly)."""
        from app.orchestrator.runner import create_run, start_run_task

        agent = await make_exposed_agent(direct_exposure=False)
        run = await create_run(None, "sneak in", mode="direct", target_sub_agent_id=agent.id)
        start_run_task(run.id)
        result = await wait_run(client, str(run.id), {"failed", "completed"})
        assert result["status"] == "failed"
        assert "not exposed" in result["error"]


class TestStaticRules:
    async def test_static_agent_exposure_toggle_allowed(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent(source="static", direct_exposure=False)
        resp = await client.patch(f"{API}/sub-agents/{agent.id}", json={"direct_exposure": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["direct_exposure"] is True
        # definition stays locked
        resp = await client.patch(f"{API}/sub-agents/{agent.id}", json={"persona": "new"})
        assert resp.status_code == 403

    async def test_exposure_listed_in_registry_out(self, client: AsyncClient) -> None:
        agent = await make_exposed_agent()
        got = (await client.get(f"{API}/sub-agents/{agent.id}")).json()
        assert got["direct_exposure"] is True
