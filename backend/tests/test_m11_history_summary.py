"""Opt-in history summary for direct invocations (spec §7.5).

The contract under test: flag off = byte-identical cold behavior; flag on =
ONE summarization call recorded as a `summary` step, worker task = summary
block + the user's verbatim message; gating 422s; retry preserves the flag;
summarization failure fails open to the cold task."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import SubAgent
from app.settings_store import update_settings
from tests.factory_helpers import create_skill, create_sub_agent

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _settings() -> None:
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "default_model": "fake:scripted",
                "formatter_enabled": False,
                "orchestrator_mode": "graph",
            },
        )


async def make_agent() -> SubAgent:
    skill = await create_skill(name=f"ctx-{uuid4().hex[:4]}")
    return await create_sub_agent(
        {
            "nodes": [{"id": "work", "type": "skill", "skill_id": str(skill.id)}],
            "edges": [{"from": "START", "to": "work"}, {"from": "work", "to": "END"}],
        },
        name=f"ctx-agent-{uuid4().hex[:4]}",
        direct_exposure=True,
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


async def seed_history(client: AsyncClient, agent_id: str) -> str:
    """One completed direct run so the conversation has summarizable history."""
    fake_llm.push_ai("the answer to the first question was 42 units")
    resp = await client.post(
        f"{API}/sub-agents/{agent_id}/invoke", json={"message": "how many units?"}
    )
    body = resp.json()
    await wait_run(client, body["run_id"], {"completed"})
    return str(body["conversation_id"])


def dispatch_task(run: dict[str, Any]) -> str:
    """The task text the worker actually received (dispatch step input)."""
    step = next(
        s
        for s in run["steps"]
        if s["step_type"] == "skill" and (s.get("input") or {}).get("task") is not None
    )
    return str(step["input"]["task"])


class TestGating:
    async def test_chat_flag_without_target_422(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{API}/chat", json={"message": "hi", "include_history_summary": True}
        )
        assert resp.status_code == 422
        assert "orchestrator always receives" in resp.json()["detail"]

    async def test_invoke_flag_without_conversation_422(self, client: AsyncClient) -> None:
        agent = await make_agent()
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke",
            json={"message": "hi", "include_history_summary": True},
        )
        assert resp.status_code == 422
        assert "requires a conversation_id" in resp.json()["detail"]


class TestSummaryOn:
    async def test_summary_step_and_composed_task(self, client: AsyncClient) -> None:
        agent = await make_agent()
        conv = await seed_history(client, str(agent.id))
        fake_llm.push_ai("SUMMARY: user previously asked unit count; answer was 42 units")
        fake_llm.push_ai("with context: still 42 units")
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke",
            json={
                "message": "and how many was that again?",
                "conversation_id": conv,
                "include_history_summary": True,
            },
        )
        assert resp.status_code == 201, resp.text
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["include_history_summary"] is True
        summary_steps = [s for s in run["steps"] if s["step_type"] == "summary"]
        assert len(summary_steps) == 1
        assert "42 units" in summary_steps[0]["output"]["summary"]
        task = dispatch_task(run)
        assert task.startswith("Conversation summary (context):")
        assert "SUMMARY: user previously asked unit count" in task
        # the user's message rides verbatim after the summary block
        assert task.endswith("Current request: and how many was that again?")

    async def test_chat_target_surface_composes_too(self, client: AsyncClient) -> None:
        agent = await make_agent()
        conv = await seed_history(client, str(agent.id))
        fake_llm.push_ai("summary text here")
        fake_llm.push_ai("contextual answer")
        resp = await client.post(
            f"{API}/chat",
            json={
                "message": "follow up please",
                "conversation_id": conv,
                "target_sub_agent_id": str(agent.id),
                "include_history_summary": True,
            },
        )
        assert resp.status_code == 201, resp.text
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "summary text here" in dispatch_task(run)

    async def test_summarizer_failure_fails_open_to_cold_task(self, client: AsyncClient) -> None:
        agent = await make_agent()
        conv = await seed_history(client, str(agent.id))
        fake_llm.push_error(RuntimeError("summarizer exploded"))
        fake_llm.push_ai("cold answer")
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke",
            json={
                "message": "follow up",
                "conversation_id": conv,
                "include_history_summary": True,
            },
        )
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert dispatch_task(run) == "follow up"  # cold task, run survived
        summary_steps = [s for s in run["steps"] if s["step_type"] == "summary"]
        assert summary_steps and summary_steps[0]["status"] == "failed"


class TestSummaryOffParity:
    async def test_flag_off_task_is_verbatim(self, client: AsyncClient) -> None:
        agent = await make_agent()
        conv = await seed_history(client, str(agent.id))
        fake_llm.push_ai("cold answer")
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke",
            json={"message": "and how many was that again?", "conversation_id": conv},
        )
        run = await wait_run(client, resp.json()["run_id"], {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["include_history_summary"] is False
        assert dispatch_task(run) == "and how many was that again?"
        assert [s for s in run["steps"] if s["step_type"] == "summary"] == []


class TestRetry:
    async def test_retry_preserves_summary_flag(self, client: AsyncClient) -> None:
        agent = await make_agent()
        conv = await seed_history(client, str(agent.id))
        # summary succeeds, worker dies → failed run with flag set
        fake_llm.push_ai("summary ok")
        fake_llm.push_error(RuntimeError("worker exploded"))
        resp = await client.post(
            f"{API}/sub-agents/{agent.id}/invoke",
            json={
                "message": "boom",
                "conversation_id": conv,
                "include_history_summary": True,
            },
        )
        run = await wait_run(client, resp.json()["run_id"], {"failed"})
        assert run["include_history_summary"] is True
        fake_llm.push_ai("summary again")
        fake_llm.push_ai("second time works")
        retry = await client.post(f"{API}/runs/{run['id']}/retry")
        assert retry.status_code == 201, retry.text
        new_run = await wait_run(client, retry.json()["run_id"], {"completed", "failed"})
        assert new_run["status"] == "completed", new_run["error"]
        assert new_run["include_history_summary"] is True
        assert "summary again" in dispatch_task(new_run)
