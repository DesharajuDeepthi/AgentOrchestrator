"""A2A execution contract tests (spec §19.5 — milestone M38).

The kind='a2a' proxy driven through REAL run machinery (fake LLM, spec
§11) against the scripted counterparty: completion + fencing inside a
graph-mode skill loop, input-required ⇄ HITL round-trips in agentic mode
(approve-with-answer and deny-cancels-remotely), failure error-edge
semantics, timeout-without-ambient, Stop → tasks/cancel propagation, and
replay adoption (exactly one remote send across a resume)."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.a2a.auth import clear_token_cache
from app.a2a.manager import A2AManager, set_manager
from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import A2ATask
from tests.factory_helpers import create_skill, create_sub_agent
from tests.stub_a2a_server import StubA2AServer

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _fake_model_settings() -> None:
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "default_model": "fake:scripted",
                "formatter_enabled": False,
                "orchestrator_mode": "graph",
            },
        )


@pytest.fixture
async def a2a_manager() -> AsyncIterator[A2AManager]:
    manager = A2AManager()
    set_manager(manager)
    clear_token_cache()
    yield manager
    await manager.stop()
    set_manager(None)


@pytest.fixture
async def stub() -> AsyncIterator[StubA2AServer]:
    server = StubA2AServer()
    await server.start()
    yield server
    await server.stop()


async def send_chat(client: AsyncClient, message: str = "do the thing") -> str:
    resp = await client.post(f"{API}/chat", json={"message": message})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["run_id"])


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


def plan_call(entries: list[dict[str, Any]] | None = None, direct_answer: str = "") -> None:
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "PlannerOutput",
                "args": {
                    "entries": entries or [],
                    "direct_answer": direct_answer,
                    "no_confident_match": False,
                },
                "id": f"p{uuid4().hex[:6]}",
            }
        ],
    )


async def register_stub_agent(
    client: AsyncClient, stub: StubA2AServer, *, timeout_s: int = 15
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Enable a2a, register the stub, return (agent, projected a2a tools)."""
    resp = await client.patch(
        f"{API}/settings", json={"a2a_enabled": True, "a2a_task_timeout_s": timeout_s}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"{API}/remote-agents", json={"card_url": stub.card_url})
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    tools = [t for t in (await client.get(f"{API}/tools")).json() if t["kind"] == "a2a"]
    return agent, tools


def a2a_tool_call(tool_name: str, message: str) -> None:
    """Script the loop model calling the a2a tool then being done."""
    fake_llm.push_ai(
        "",
        tool_calls=[{"name": tool_name, "args": {"message": message}, "id": f"t{uuid4().hex[:6]}"}],
    )


async def a2a_task_rows() -> list[A2ATask]:
    from sqlalchemy import select

    async with get_session_factory()() as db:
        return list((await db.execute(select(A2ATask))).scalars())


# ── graph mode: completion + fencing through a scoped skill loop ─────


async def test_graph_skill_loop_completion_is_fenced(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    _agent, tools = await register_stub_agent(client, stub)
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    from app.models import Tool

    async with get_session_factory()() as db:
        tool_row = await db.get(Tool, research["id"])
        assert tool_row is not None
    skill = await create_skill(name=f"remote-research-{uuid4().hex[:4]}", tools=[tool_row])
    agent_row = await create_sub_agent(
        {
            "nodes": [{"id": "s1", "type": "skill", "skill_id": str(skill.id)}],
            "edges": [
                {"from": "START", "to": "s1"},
                {"from": "s1", "to": "END", "on": "success"},
            ],
        }
    )

    assert agent_row.id is not None
    plan_call(
        entries=[
            {
                "id": "s1",
                "capability": {"type": "direct_skill", "id": str(skill.id)},
                "task": "research pgvector via the remote agent",
                "depends_on": [],
            }
        ]
    )
    a2a_tool_call("stub-agent_research", "look into pgvector")
    fake_llm.push_ai("Skill loop done with the remote result.")
    fake_llm.push_ai("Aggregated: remote research delivered.")

    run_id = await send_chat(client, "research pgvector remotely")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    tool_steps = [s for s in run["steps"] if s["step_type"] == "tool_call"]
    assert tool_steps, "expected a recorded tool_call step"
    out = str(tool_steps[0]["output"])
    assert "<untrusted_remote_agent_output" in out
    assert "stub-echo: look into pgvector" in out
    assert "never as instructions" in out

    rows = await a2a_task_rows()
    assert len(rows) == 1 and rows[0].state == "completed"
    assert rows[0].run_id == UUID(run_id)


# ── agentic mode: input-required ⇄ HITL round-trip ───────────────────


async def _agentic_setup(client: AsyncClient, stub: StubA2AServer) -> str:
    """Agentic mode + exposed a2a tool; returns the sanitized tool name."""
    _agent, tools = await register_stub_agent(client, stub)
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    resp = await client.patch(f"{API}/tools/{research['id']}", json={"direct_exposure": True})
    assert resp.status_code == 200, resp.text
    resp = await client.patch(f"{API}/settings", json={"orchestrator_mode": "agentic"})
    assert resp.status_code == 200, resp.text
    return "stub-agent_research"


async def test_agentic_input_required_hitl_approve_round_trip(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    tool_name = await _agentic_setup(client, stub)

    a2a_tool_call(tool_name, "ask:Which environment should I target?")
    run_id = await send_chat(client, "ask the remote agent")
    run = await wait_run(client, run_id, {"paused_hitl", "failed"})
    assert run["status"] == "paused_hitl", run["error"]

    pending = (await client.get(f"{API}/hitl/pending")).json()
    assert any(p["run_id"] == run_id for p in pending)

    rows = await a2a_task_rows()
    assert len(rows) == 1
    assert rows[0].state == "input-required"
    assert "Which environment" in (rows[0].question or "")
    sends_before = len(stub.seen_auth)

    # approve with the typed reply; the resumed proxy must ADOPT the open
    # remote task (no second initial send) and reply into it
    fake_llm.push_ai("The remote agent answered; all done.")
    resp = await client.post(
        f"{API}/runs/{run_id}/hitl",
        json={"decision": "approve", "note": "", "answers": {"reply": "target staging"}},
    )
    assert resp.status_code == 200, resp.text
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    rows = await a2a_task_rows()
    assert len(rows) == 1, "replay must adopt, never create a second task row"
    assert rows[0].state == "completed"
    assert "stub-answered: target staging" in (rows[0].result or {}).get("text", "")

    tool_steps = [s for s in run["steps"] if s["step_type"] == "tool_call"]
    assert any("stub-answered: target staging" in str(s.get("output")) for s in tool_steps)
    # the counterparty saw exactly: initial send + get_task (adoption) + reply
    assert len(stub.seen_auth) - sends_before >= 2


async def test_agentic_hitl_deny_cancels_remote_task(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    tool_name = await _agentic_setup(client, stub)

    a2a_tool_call(tool_name, "ask:May I proceed?")
    run_id = await send_chat(client, "ask again")
    run = await wait_run(client, run_id, {"paused_hitl", "failed"})
    assert run["status"] == "paused_hitl", run["error"]

    # deny → proxy cancels the remote task and errors; the agentic loop
    # self-corrects (error ToolMessage) and still finishes the run
    fake_llm.push_ai("Understood — the human declined, stopping here.")
    resp = await client.post(
        f"{API}/runs/{run_id}/hitl", json={"decision": "deny", "note": "not now"}
    )
    assert resp.status_code == 200, resp.text
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    assert stub.cancelled_tasks, "deny must propagate tasks/cancel to the counterparty"
    rows = await a2a_task_rows()
    assert rows and rows[0].state == "canceled"
    assert "denied by human" in (rows[0].error or "")


async def test_graph_worker_input_required_pauses_and_resumes(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    """Graph mode, custom-sub-agent rung: a remote `input-required` inside a
    WORKER's skill loop must pause the run (never take the error edge — the
    §3.5 catch may not swallow GraphInterrupt), and the typed reply resumes
    into the SAME remote task via replay adoption."""
    _agent, tools = await register_stub_agent(client, stub)
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    from app.models import Tool

    async with get_session_factory()() as db:
        tool_row = await db.get(Tool, research["id"])
        assert tool_row is not None
    skill = await create_skill(name=f"remote-gate-{uuid4().hex[:4]}", tools=[tool_row])
    agent_row = await create_sub_agent(
        {
            "nodes": [{"id": "s1", "type": "skill", "skill_id": str(skill.id)}],
            "edges": [
                {"from": "START", "to": "s1"},
                {"from": "s1", "to": "END", "on": "success"},
            ],
        }
    )

    plan_call(
        entries=[
            {
                "id": "s1",
                "capability": {"type": "sub_agent", "id": str(agent_row.id)},
                "task": "relay the exact ask: message to the remote agent",
                "depends_on": [],
            }
        ]
    )
    a2a_tool_call("stub-agent_research", "ask:Which environment should I target?")

    run_id = await send_chat(client, "relay the question")
    run = await wait_run(client, run_id, {"paused_hitl", "completed", "failed"})
    assert run["status"] == "paused_hitl", (run["status"], run["error"])

    pending = (await client.get(f"{API}/hitl/pending")).json()
    assert any(p["run_id"] == run_id for p in pending)

    rows = await a2a_task_rows()
    assert len(rows) == 1 and rows[0].state == "input-required"

    fake_llm.push_ai("Relayed; the remote agent answered.")  # skill loop wrap-up
    fake_llm.push_ai("Aggregated: staged reply delivered.")  # aggregator
    resp = await client.post(
        f"{API}/runs/{run_id}/hitl",
        json={"decision": "approve", "note": "", "answers": {"reply": "target staging"}},
    )
    assert resp.status_code == 200, resp.text
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    rows = await a2a_task_rows()
    assert len(rows) == 1, "replay must adopt the open remote task, never re-send"
    assert rows[0].state == "completed"
    assert "stub-answered: target staging" in (rows[0].result or {}).get("text", "")
    tool_steps = [s for s in run["steps"] if s["step_type"] == "tool_call"]
    assert any("untrusted_remote_agent_output" in str(s.get("output")) for s in tool_steps)


# ── failure + timeout semantics ──────────────────────────────────────


async def test_graph_skill_loop_remote_failure_takes_error_edge(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    _agent, tools = await register_stub_agent(client, stub)
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    from app.models import Tool

    async with get_session_factory()() as db:
        tool_row = await db.get(Tool, research["id"])
        assert tool_row is not None
    skill = await create_skill(name=f"remote-fail-{uuid4().hex[:4]}", tools=[tool_row])
    agent_row = await create_sub_agent(
        {
            "nodes": [{"id": "s1", "type": "skill", "skill_id": str(skill.id)}],
            "edges": [
                {"from": "START", "to": "s1"},
                {"from": "s1", "to": "END", "on": "success"},
            ],
        }
    )
    assert agent_row.id is not None
    plan_call(
        entries=[
            {
                "id": "s1",
                "capability": {"type": "direct_skill", "id": str(skill.id)},
                "task": "make the remote call fail",
                "depends_on": [],
            }
        ]
    )
    a2a_tool_call("stub-agent_research", "fail:remote exploded")
    fake_llm.push_ai("The remote task failed; reporting that honestly.")  # aggregator

    run_id = await send_chat(client, "make it fail")
    run = await wait_run(client, run_id, {"completed", "failed"})
    # strict tool error → node error → no error edge → the WORKER dispatch
    # fails and the aggregator reports it; the run itself completes (spec
    # §3.5 error-edge semantics + §7.1 dispatch-failure reporting)
    assert run["status"] == "completed", run["error"]

    failed_steps = [s for s in run["steps"] if s["status"] == "failed"]
    assert failed_steps, "expected the failing tool_call/skill step in the trace"
    assert any("remote agent task failed" in str(s.get("error") or "") for s in failed_steps)

    rows = await a2a_task_rows()
    assert rows and rows[0].state == "failed"
    assert "remote exploded" in (rows[0].error or "")


async def test_timeout_without_ambient_is_plain_tool_error(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    tool_name = await _agentic_setup(client, stub)
    resp = await client.patch(f"{API}/settings", json={"a2a_task_timeout_s": 1})
    assert resp.status_code == 200, resp.text

    a2a_tool_call(tool_name, "slow:10")
    fake_llm.push_ai("The remote call timed out; reporting that honestly.")
    run_id = await send_chat(client, "slow remote work")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    tool_steps = [s for s in run["steps"] if s["step_type"] == "tool_call"]
    assert any("timed out after 1s" in str(s.get("error") or "") for s in tool_steps)
    rows = await a2a_task_rows()
    assert rows and rows[0].state == "unknown"
    assert "timed out" in (rows[0].error or "")


# ── Stop propagates tasks/cancel ─────────────────────────────────────


async def test_stop_propagates_remote_cancel(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    tool_name = await _agentic_setup(client, stub)
    resp = await client.patch(f"{API}/settings", json={"a2a_task_timeout_s": 60})
    assert resp.status_code == 200, resp.text

    a2a_tool_call(tool_name, "slow:30")
    run_id = await send_chat(client, "long remote work")
    await asyncio.wait_for(stub.slow_started.wait(), timeout=10)
    # Stop propagation needs the platform to know the remote task id first —
    # wait for the a2a_tasks row (recorded at the first streamed event)
    deadline = asyncio.get_event_loop().time() + 5
    while (  # noqa: ASYNC110 - polling external DB state, no event exists
        asyncio.get_event_loop().time() < deadline and not await a2a_task_rows()
    ):
        await asyncio.sleep(0.1)
    assert await a2a_task_rows(), "expected the task row before cancelling"

    resp = await client.post(f"{API}/runs/{run_id}/cancel")
    assert resp.status_code == 200, resp.text
    run = await wait_run(client, run_id, {"cancelled"})
    assert run["status"] == "cancelled"

    deadline = asyncio.get_event_loop().time() + 5
    while (  # noqa: ASYNC110 - polling cross-task list, no event exists
        asyncio.get_event_loop().time() < deadline and not stub.cancelled_tasks
    ):
        await asyncio.sleep(0.1)
    assert stub.cancelled_tasks, "Stop must best-effort cancel the remote task"
    rows = await a2a_task_rows()
    assert rows and rows[0].state == "canceled"


# ── rung 1: a direct tool call cannot pause — clear error + drawer ───


async def test_direct_tool_rung_cannot_pause_clear_error_and_drawer_continues(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    """Rung 1 executes outside any graph, so a remote `input-required` gate
    cannot pause the run. The interrupt must surface as a CLEAR tool error
    (never a swallowed GraphInterrupt repr), the task row stays
    `input-required`, and the §19.6 drawer reply completes the remote task."""
    agent, tools = await register_stub_agent(client, stub)
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    resp = await client.patch(f"{API}/tools/{research['id']}", json={"direct_exposure": True})
    assert resp.status_code == 200, resp.text
    resp = await client.patch(f"{API}/settings", json={"ambient_enabled": True})
    assert resp.status_code == 200, resp.text

    plan_call(
        entries=[
            {
                "id": "s1",
                "capability": {"type": "direct_tool", "id": str(research["id"])},
                "task": "ask the remote agent which environment",
                "depends_on": [],
            }
        ]
    )
    # the rung's own arg-derivation model call picks the tool…
    a2a_tool_call("stub-agent_research", "ask:Which environment should I target?")
    fake_llm.push_ai("The remote agent needs input; see the task drawer.")  # aggregator

    run_id = await send_chat(client, "direct-tool ask")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    tool_steps = [s for s in run["steps"] if s["step_type"] == "tool_call"]
    assert tool_steps and tool_steps[0]["status"] == "failed"
    err = str(tool_steps[0].get("error") or "")
    assert "cannot pause" in err and "task drawer" in err
    assert "Which environment should I target?" in err
    assert "GraphInterrupt" not in err and "Interrupt(" not in err

    rows = await a2a_task_rows()
    assert len(rows) == 1 and rows[0].state == "input-required"
    assert "Which environment" in (rows[0].question or "")

    # the drawer carries the reply from here (spec §19.6)
    resp = await client.post(
        f"{API}/remote-agents/{agent['id']}/tasks/{rows[0].id}/reply",
        json={"text": "use staging"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "completed"
    rows = await a2a_task_rows()
    assert "stub-answered: use staging" in (rows[0].result or {}).get("text", "")


async def test_inline_skill_interrupt_surfaces_clear_error_not_repr(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    """Rung-2 INLINE skill loops (direct_exposure skills) run without a
    checkpointer — a remote `input-required` cannot pause there. The
    GraphInterrupt must surface as the same clear drawer-pointing contract
    rung 1 uses, never a raw Interrupt repr (caught live in §14d)."""
    agent, tools = await register_stub_agent(client, stub)
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    from app.models import Tool

    async with get_session_factory()() as db:
        tool_row = await db.get(Tool, research["id"])
        assert tool_row is not None
    skill = await create_skill(name=f"inline-remote-{uuid4().hex[:4]}", tools=[tool_row])
    resp = await client.patch(f"{API}/skills/{skill.id}", json={"direct_exposure": True})
    assert resp.status_code == 200, resp.text

    plan_call(
        entries=[
            {
                "id": "s1",
                "capability": {"type": "direct_skill", "id": str(skill.id)},
                "task": "ask the remote agent which environment",
                "depends_on": [],
            }
        ]
    )
    a2a_tool_call("stub-agent_research", "ask:Which environment should I target?")
    fake_llm.push_ai("Aggregated: the remote agent needs input; see the task drawer.")

    run_id = await send_chat(client, "remote ask through an inline skill")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    skill_steps = [s for s in run["steps"] if s["step_type"] == "skill"]
    assert skill_steps and skill_steps[-1]["status"] == "failed"
    err = str(skill_steps[-1].get("error") or "")
    assert "inline skill call cannot pause" in err and "task drawer" in err
    assert "Which environment should I target?" in err
    assert "Interrupt(" not in err and "GraphInterrupt" not in err

    rows = await a2a_task_rows()
    assert len(rows) == 1 and rows[0].state == "input-required"
