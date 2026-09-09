"""A2A long-running contract tests (spec §19.6 — milestone M39).

Park-on-budget, the ambient leader-tick poller delivering through the
outbox with NO recheck run, the input-required-while-parked drawer
round-trip, drawer cancel, the park cap, and poller inertness with a2a
dark. The poller is called directly (the drain loop invokes the same
function on the leader tick)."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.a2a.auth import clear_token_cache
from app.a2a.manager import A2AManager, set_manager
from app.a2a.poller import poll_parked_tasks, reset_poll_watermark
from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import A2ATask, Delivery, Run
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
                "orchestrator_mode": "agentic",
                "ambient_enabled": True,
            },
        )


@pytest.fixture
async def a2a_manager() -> AsyncIterator[A2AManager]:
    manager = A2AManager()
    set_manager(manager)
    clear_token_cache()
    reset_poll_watermark()
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


async def setup_agentic_tool(
    client: AsyncClient, stub: StubA2AServer, *, timeout_s: int = 1
) -> tuple[str, str]:
    resp = await client.patch(
        f"{API}/settings",
        # interval 1: these tests drive the poller directly with sub-minute
        # sleeps — the M40 watermark must not throttle them
        json={"a2a_enabled": True, "a2a_task_timeout_s": timeout_s, "a2a_poll_interval_s": 1},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"{API}/remote-agents", json={"card_url": stub.card_url})
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]
    tools = [t for t in (await client.get(f"{API}/tools")).json() if t["kind"] == "a2a"]
    research = next(t for t in tools if t["tool_key"] == "stub-agent.research")
    resp = await client.patch(f"{API}/tools/{research['id']}", json={"direct_exposure": True})
    assert resp.status_code == 200, resp.text
    return agent_id, "stub-agent_research"


def tool_call(tool_name: str, message: str) -> None:
    fake_llm.push_ai(
        "",
        tool_calls=[{"name": tool_name, "args": {"message": message}, "id": f"t{uuid4().hex[:6]}"}],
    )


async def task_rows() -> list[A2ATask]:
    async with get_session_factory()() as db:
        return list((await db.execute(select(A2ATask))).scalars())


async def a2a_deliveries() -> list[Delivery]:
    async with get_session_factory()() as db:
        return list(
            (await db.execute(select(Delivery).where(Delivery.category == "a2a"))).scalars()
        )


async def run_count() -> int:
    from sqlalchemy import func

    async with get_session_factory()() as db:
        return int((await db.execute(select(func.count()).select_from(Run))).scalar_one())


async def park_slow_task(
    client: AsyncClient, stub: StubA2AServer, tool_name: str, message: str
) -> str:
    """Drive a run whose remote call outlives the 1s budget and parks."""
    tool_call(tool_name, message)
    fake_llm.push_ai("Task parked; I am done for now.")
    run_id = await send_chat(client, "long remote job")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]
    rows = await task_rows()
    assert len(rows) == 1 and rows[0].state == "parked", rows
    tool_steps = [s for s in run["steps"] if s["step_type"] == "tool_call"]
    assert any("parked" in str(s.get("output")) for s in tool_steps)
    return run_id


# ── park → poll → deliver (spec §19.6) ───────────────────────────────


async def test_park_then_poller_delivers_with_no_recheck_run(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    _agent_id, tool_name = await setup_agentic_tool(client, stub)
    await park_slow_task(client, stub, tool_name, "slow:3")
    runs_before = await run_count()

    # too early — remote still working: row stays parked, nothing delivered
    assert await poll_parked_tasks() == 0
    rows = await task_rows()
    assert rows[0].state == "parked"

    await asyncio.sleep(3.2)  # let the counterparty finish
    assert await poll_parked_tasks() == 1

    rows = await task_rows()
    assert rows[0].state == "completed" and rows[0].delivered is True
    assert "stub-slow-done" in (rows[0].result or {}).get("text", "")

    deliveries = await a2a_deliveries()
    assert len(deliveries) == 1
    d = deliveries[0]
    assert d.tier == 2
    assert d.skey == f"a2a:{rows[0].id}"
    assert "<untrusted_remote_agent_output" in (d.body or "")
    assert "stub-slow-done" in (d.body or "")
    assert await run_count() == runs_before  # no recheck run, ever


async def test_parked_input_required_delivers_question_then_drawer_reply(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    agent_id, tool_name = await setup_agentic_tool(client, stub)
    await park_slow_task(client, stub, tool_name, "slowask:3:Which dataset should I use?")

    await asyncio.sleep(3.2)
    assert await poll_parked_tasks() == 1

    rows = await task_rows()
    assert rows[0].state == "input-required"
    assert "Which dataset" in (rows[0].question or "")
    deliveries = await a2a_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].tier == 1
    assert "needs your input" in deliveries[0].title
    assert "Which dataset" in (deliveries[0].body or "")

    # a second poller pass leaves input-required rows to the drawer —
    # watermark reset so this proves drawer semantics, not throttling
    reset_poll_watermark()
    assert await poll_parked_tasks() == 0

    # reply from the task drawer → the stub completes → result delivered
    resp = await client.post(
        f"{API}/remote-agents/{agent_id}/tasks/{rows[0].id}/reply",
        json={"text": "use the staging dataset"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "completed"

    rows = await task_rows()
    assert rows[0].delivered is True
    assert "stub-answered: use the staging dataset" in (rows[0].result or {}).get("text", "")
    deliveries = await a2a_deliveries()
    # the result delivery supersede-collapses onto the same skey lineage
    assert any("remote task completed" in d.title for d in deliveries)


async def test_drawer_cancel_propagates(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    agent_id, tool_name = await setup_agentic_tool(client, stub)
    await park_slow_task(client, stub, tool_name, "slow:30")

    rows = await task_rows()
    resp = await client.post(f"{API}/remote-agents/{agent_id}/tasks/{rows[0].id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "canceled"
    assert stub.cancelled_tasks, "drawer cancel must reach the counterparty"

    # cancelled rows leave the poller's queue
    assert await poll_parked_tasks() == 0


# ── guardrails ───────────────────────────────────────────────────────


async def test_park_cap_zero_turns_timeout_into_error(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    _agent_id, tool_name = await setup_agentic_tool(client, stub)
    resp = await client.patch(f"{API}/settings", json={"a2a_max_parked": 0})
    assert resp.status_code == 200, resp.text

    tool_call(tool_name, "slow:10")
    fake_llm.push_ai("The remote call timed out; nothing was parked.")
    run_id = await send_chat(client, "no parking allowed")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]

    rows = await task_rows()
    assert rows and rows[0].state == "unknown"
    assert "timed out" in (rows[0].error or "")
    assert not await a2a_deliveries()


async def test_poller_inert_while_a2a_dark(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    _agent_id, tool_name = await setup_agentic_tool(client, stub)
    await park_slow_task(client, stub, tool_name, "slow:2")
    await asyncio.sleep(2.2)

    resp = await client.patch(f"{API}/settings", json={"a2a_enabled": False})
    assert resp.status_code == 200, resp.text
    assert await poll_parked_tasks() == 0  # dark ⇒ no rechecks, no deliveries
    rows = await task_rows()
    assert rows[0].state == "parked"
    assert not await a2a_deliveries()


async def test_poller_honors_a2a_poll_interval(
    client: AsyncClient, a2a_manager: A2AManager, stub: StubA2AServer
) -> None:
    """M40: `a2a_poll_interval_s` is tick-bounded — a pass within the
    interval no-ops even with a settled task waiting; a live settings
    change to a shorter interval applies immediately."""
    _agent_id, tool_name = await setup_agentic_tool(client, stub)
    await park_slow_task(client, stub, tool_name, "slow:1")
    await asyncio.sleep(1.2)  # remote finished; row still parked

    # a long interval throttles the SECOND pass even though work waits
    resp = await client.patch(f"{API}/settings", json={"a2a_poll_interval_s": 3600})
    assert resp.status_code == 200, resp.text
    reset_poll_watermark()
    assert await poll_parked_tasks() == 1  # first pass after reset runs

    tool_call(tool_name, "slow:1.5")
    fake_llm.push_ai("Second task parked; done for now.")
    run_id = await send_chat(client, "another long remote job")
    run = await wait_run(client, run_id, {"completed", "failed"})
    assert run["status"] == "completed", run["error"]
    await asyncio.sleep(1.7)
    assert await poll_parked_tasks() == 0  # throttled — interval not elapsed

    # shortening the interval applies live, no restart
    resp = await client.patch(f"{API}/settings", json={"a2a_poll_interval_s": 1})
    assert resp.status_code == 200, resp.text
    await asyncio.sleep(1.1)
    assert await poll_parked_tasks() == 1
