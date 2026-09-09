"""A2A substrate contract tests (spec §19.2–19.4, §19.7 — milestone M37).

Run against the scripted in-process counterparty (stub_a2a_server) —
deterministic and key-free, the §11 discipline. Covers: dark-mode byte
identity, register → card → tools projection, refresh drift, the full
auth matrix (incl. env indirection + the oauth2 token cache), write-only
credentials, delete cascade + dependents, and tool_key collisions."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.a2a.auth import clear_token_cache
from app.a2a.manager import A2AManager, set_manager
from app.db import get_session_factory
from app.models import A2ATask, RemoteAgent, Tool
from tests.stub_a2a_server import OAUTH_TOKEN, StubA2AServer


@pytest.fixture
async def a2a_manager() -> AsyncIterator[A2AManager]:
    manager = A2AManager()
    set_manager(manager)
    clear_token_cache()
    yield manager
    await manager.stop()
    set_manager(None)


async def make_stub(auth: str | None = None, **kwargs: Any) -> StubA2AServer:
    stub = StubA2AServer(auth=auth, **kwargs)
    await stub.start()
    return stub


async def enable_a2a(client: AsyncClient) -> None:
    resp = await client.patch("/api/v1/settings", json={"a2a_enabled": True})
    assert resp.status_code == 200, resp.text


async def register(
    client: AsyncClient, stub: StubA2AServer, credentials: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"card_url": stub.card_url}
    if credentials is not None:
        body["credentials"] = credentials
    resp = await client.post("/api/v1/remote-agents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── dark by default (spec §19, §11 byte identity) ────────────────────


async def test_dark_mode_writes_409_and_no_rows(
    client: AsyncClient, a2a_manager: A2AManager
) -> None:
    resp = await client.post("/api/v1/remote-agents", json={"card_url": "http://x/card.json"})
    assert resp.status_code == 409
    assert "a2a_enabled" in resp.json()["detail"]
    resp = await client.get("/api/v1/remote-agents")
    assert resp.status_code == 200
    assert resp.json() == []
    async with get_session_factory()() as db:
        agents = (await db.execute(select(func.count()).select_from(RemoteAgent))).scalar_one()
        tasks = (await db.execute(select(func.count()).select_from(A2ATask))).scalar_one()
    assert agents == 0 and tasks == 0


async def test_dark_mode_settings_default_off(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/settings")
    assert resp.json()["a2a_enabled"] is False


# ── register → card → tools projection (spec §19.2/§19.4) ────────────


async def test_register_projects_card_skills_as_tools(
    client: AsyncClient, a2a_manager: A2AManager
) -> None:
    stub = await make_stub()
    try:
        await enable_a2a(client)
        agent = await register(client, stub)
        assert agent["name"] == "stub-agent"
        assert agent["status"] == "active"
        assert agent["tool_count"] == 2
        assert agent["auth_status"] == "open"
        assert agent["card"]["name"] == "stub-agent"

        resp = await client.get("/api/v1/tools")
        rows = [t for t in resp.json() if t["kind"] == "a2a"]
        assert {t["tool_key"] for t in rows} == {"stub-agent.research", "stub-agent.summarize"}
        for t in rows:
            assert t["remote_agent_id"] == agent["id"]
            assert t["input_schema"]["required"] == ["message"]
            assert t["status"] == "active"
    finally:
        await stub.stop()


async def test_card_refresh_reflects_drift(client: AsyncClient, a2a_manager: A2AManager) -> None:
    from a2a.types import AgentSkill

    stub = await make_stub()
    try:
        await enable_a2a(client)
        agent = await register(client, stub)

        stub.skills.append(
            AgentSkill(id="translate", name="translate", description="Translate text", tags=[])
        )
        resp = await client.post(f"/api/v1/remote-agents/{agent['id']}/refresh-card")
        assert resp.status_code == 200
        assert resp.json()["tool_count"] == 3

        stub.skills = [s for s in stub.skills if s.id != "research"]
        await client.post(f"/api/v1/remote-agents/{agent['id']}/refresh-card")
        resp = await client.get("/api/v1/tools")
        by_key = {t["tool_key"]: t for t in resp.json() if t["kind"] == "a2a"}
        assert by_key["stub-agent.research"]["status"] == "inactive"
        assert by_key["stub-agent.translate"]["status"] == "active"
    finally:
        await stub.stop()


async def test_tool_key_collision_suffixed(client: AsyncClient, a2a_manager: A2AManager) -> None:
    stub = await make_stub()
    try:
        await enable_a2a(client)
        await register(client, stub)
        await register(client, stub)  # same card again → same names
        async with get_session_factory()() as db:
            keys = list(
                (await db.execute(select(Tool.tool_key).where(Tool.kind == "a2a"))).scalars()
            )
        plain = [k for k in keys if k == "stub-agent.research"]
        suffixed = [k for k in keys if k.startswith("stub-agent.research-")]
        assert len(plain) == 1 and len(suffixed) == 1
    finally:
        await stub.stop()


# ── auth matrix (spec §19.3/§19.7) ───────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "credential"),
    [
        ("apikey-header", "stub-api-key"),
        ("apikey-query", "stub-api-key"),
        ("apikey-cookie", "stub-api-key"),
        ("basic", "stub-user:stub-pass"),
        ("bearer", "stub-bearer-token"),
        ("oauth2", {"client_id": "stub-client", "client_secret": "stub-client-secret"}),
    ],
)
async def test_auth_matrix_placement(
    client: AsyncClient, a2a_manager: A2AManager, mode: str, credential: Any
) -> None:
    from uuid import UUID

    from app.a2a.client_port import send_text

    stub = await make_stub(auth=mode)
    try:
        await enable_a2a(client)
        agent = await register(client, stub, credentials={"main": credential})
        assert agent["auth_status"] == "ok"
        outcome = await send_text(UUID(agent["id"]), "ping")
        assert outcome.state == "completed", outcome
        assert "stub-echo: ping" in outcome.text
        seen = stub.seen_auth[-1]
        match mode:
            case "apikey-header":
                assert seen["x_api_key"] == "stub-api-key"
            case "apikey-query":
                assert seen["query_api_key"] == "stub-api-key"
            case "apikey-cookie":
                assert seen["cookie"] == "stub-api-key"
            case "basic":
                assert seen["authorization"].startswith("Basic ")
            case "bearer":
                assert seen["authorization"] == "Bearer stub-bearer-token"
            case "oauth2":
                assert seen["authorization"] == f"Bearer {OAUTH_TOKEN}"
    finally:
        await stub.stop()


async def test_oauth2_token_cache_reuses_token(
    client: AsyncClient, a2a_manager: A2AManager
) -> None:
    from uuid import UUID

    from app.a2a.client_port import send_text

    stub = await make_stub(auth="oauth2")
    try:
        await enable_a2a(client)
        agent = await register(
            client,
            stub,
            credentials={
                "main": {"client_id": "stub-client", "client_secret": "stub-client-secret"}
            },
        )
        await send_text(UUID(agent["id"]), "one")
        await send_text(UUID(agent["id"]), "two")
        assert stub.token_requests == 1  # second call rides the cache
    finally:
        await stub.stop()


async def test_env_var_indirection(
    client: AsyncClient, a2a_manager: A2AManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import UUID

    from app.a2a.client_port import send_text

    monkeypatch.setenv("STUB_A2A_KEY", "stub-api-key")
    stub = await make_stub(auth="apikey-header")
    try:
        await enable_a2a(client)
        agent = await register(client, stub, credentials={"main": "env:STUB_A2A_KEY"})
        outcome = await send_text(UUID(agent["id"]), "ping")
        assert outcome.state == "completed"
    finally:
        await stub.stop()


async def test_wrong_credentials_fail_loudly(client: AsyncClient, a2a_manager: A2AManager) -> None:
    from uuid import UUID

    from app.a2a.client_port import send_text

    stub = await make_stub(auth="bearer")
    try:
        await enable_a2a(client)
        agent = await register(client, stub, credentials={"main": "wrong-token"})
        with pytest.raises(Exception) as excinfo:
            await send_text(UUID(agent["id"]), "ping")
        assert "401" in str(excinfo.value)
    finally:
        await stub.stop()


async def test_unsupported_scheme_surfaces(client: AsyncClient, a2a_manager: A2AManager) -> None:
    stub = await make_stub(auth="mtls-only")
    try:
        await enable_a2a(client)
        agent = await register(client, stub)
        assert agent["auth_status"] == "unsupported"
        assert agent["auth"]["main"]["supported"] is False
    finally:
        await stub.stop()


async def test_missing_credentials_surface_unconfigured(
    client: AsyncClient, a2a_manager: A2AManager
) -> None:
    stub = await make_stub(auth="bearer")
    try:
        await enable_a2a(client)
        agent = await register(client, stub)
        assert agent["auth_status"] == "unconfigured"
        assert agent["auth"]["main"] == {
            "type": "http",
            "supported": True,
            "configured": False,
        }
    finally:
        await stub.stop()


# ── credentials are write-only (spec §19.3) ──────────────────────────


async def test_credentials_never_echoed(client: AsyncClient, a2a_manager: A2AManager) -> None:
    stub = await make_stub(auth="bearer")
    try:
        await enable_a2a(client)
        agent = await register(client, stub, credentials={"main": "super-secret-token"})
        for resp in (
            await client.get("/api/v1/remote-agents"),
            await client.get(f"/api/v1/remote-agents/{agent['id']}"),
            await client.patch(f"/api/v1/remote-agents/{agent['id']}", json={"description": "x"}),
        ):
            body = resp.text
            assert "super-secret-token" not in body
            assert '"credentials"' not in body
        # patch-merge: set a second scheme, clear the first
        resp = await client.patch(
            f"/api/v1/remote-agents/{agent['id']}",
            json={"credentials": {"main": None, "other": "x"}},
        )
        assert resp.status_code == 200
        async with get_session_factory()() as db:
            row = await db.get(RemoteAgent, agent["id"])
            assert row is not None
            assert row.credentials == {"other": "x"}
    finally:
        await stub.stop()


# ── delete cascade + dependents (spec §19.2, MCP parity) ─────────────


async def test_delete_cascades_and_respects_dependents(
    client: AsyncClient, a2a_manager: A2AManager
) -> None:
    stub = await make_stub()
    try:
        await enable_a2a(client)
        agent = await register(client, stub)
        resp = await client.get("/api/v1/tools")
        tool_id = next(t["id"] for t in resp.json() if t["kind"] == "a2a")

        resp = await client.post(
            "/api/v1/skills",
            json={
                "name": "remote-research",
                "description": "uses the remote agent",
                "instructions": "Call {tool:stub-agent.research} to research the topic.",
                "tool_ids": [tool_id],
            },
        )
        assert resp.status_code == 201, resp.text
        skill_id = resp.json()["id"]

        resp = await client.delete(f"/api/v1/remote-agents/{agent['id']}")
        assert resp.status_code == 409
        assert "remote-research" in resp.json()["detail"]

        await client.delete(f"/api/v1/skills/{skill_id}")
        resp = await client.delete(f"/api/v1/remote-agents/{agent['id']}")
        assert resp.status_code == 204

        resp = await client.get("/api/v1/tools")
        assert not [t for t in resp.json() if t["kind"] == "a2a"]
        resp = await client.get("/api/v1/remote-agents")
        assert resp.json() == []
    finally:
        await stub.stop()


# ── bad card URL ─────────────────────────────────────────────────────


async def test_unreachable_card_is_422(client: AsyncClient, a2a_manager: A2AManager) -> None:
    await enable_a2a(client)
    resp = await client.post(
        "/api/v1/remote-agents",
        json={"card_url": "http://127.0.0.1:9/absent/agent-card.json"},
    )
    assert resp.status_code == 422
    assert "could not fetch agent card" in resp.json()["detail"]
