"""MCP connection manager (spec §5) against a stub MCP server: connect,
ingest, listChanged reconcile, error status, health, invocation."""

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db import get_session_factory
from app.mcp.manager import McpManager, set_manager
from app.models import McpServer, Tool

STUB = str(Path(__file__).resolve().parent / "stub_mcp_server.py")
API = "/api/v1"


@pytest.fixture
async def manager() -> AsyncIterator[McpManager]:
    m = McpManager()
    set_manager(m)
    yield m
    set_manager(None)
    await m.stop()


async def make_stub_server(extra_args: list[str] | None = None, **overrides: Any) -> UUID:
    async with get_session_factory()() as session:
        fields: dict[str, Any] = {
            "name": overrides.pop("name", "stub"),
            "description": "stub server",
            "transport": "stdio",
            "command": sys.executable,
            "args": [STUB, *(extra_args or [])],
            "source": "dynamic",
            "status": "inactive",
        }
        fields.update(overrides)
        server = McpServer(**fields)
        session.add(server)
        await session.commit()
        return server.id


async def get_server(server_id: UUID) -> McpServer:
    async with get_session_factory()() as session:
        server = await session.get(McpServer, server_id)
        assert server is not None
        return server


async def tools_of(server_id: UUID) -> dict[str, Tool]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(Tool).where(Tool.mcp_server_id == server_id))
        ).scalars()
        return {t.tool_name: t for t in rows}


async def wait_for(predicate: Any, timeout_s: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not met within timeout")


class TestConnectAndIngest:
    async def test_stdio_connect_ingests_tools(self, manager: McpManager) -> None:
        server_id = await make_stub_server()
        await manager.connect_server(server_id)

        server = await get_server(server_id)
        assert server.status == "active"
        assert server.last_connected_at is not None
        assert server.last_error is None

        tools = await tools_of(server_id)
        assert {"echo", "add"} <= set(tools)
        assert tools["echo"].tool_key == "stub.echo"
        assert tools["echo"].kind == "mcp"
        assert tools["echo"].source == "dynamic"  # inherited from server
        assert tools["echo"].input_schema is not None
        assert "text" in tools["echo"].input_schema["properties"]

    async def test_static_server_tools_inherit_static(self, manager: McpManager) -> None:
        server_id = await make_stub_server(name="stub-static", source="static")
        await manager.connect_server(server_id)
        tools = await tools_of(server_id)
        assert tools["echo"].source == "static"

    async def test_refresh_idempotent_ids_stable(self, manager: McpManager) -> None:
        server_id = await make_stub_server()
        await manager.connect_server(server_id)
        before = {name: t.id for name, t in (await tools_of(server_id)).items()}
        await manager.refresh_tools(server_id)
        after = {name: t.id for name, t in (await tools_of(server_id)).items()}
        assert before == after

    async def test_connect_failure_sets_error(self, manager: McpManager) -> None:
        server_id = await make_stub_server(extra_args=["--fail"])
        await manager.connect_server(server_id)
        server = await get_server(server_id)
        assert server.status == "error"
        assert server.last_error

    async def test_reconnect_after_fix(self, manager: McpManager) -> None:
        server_id = await make_stub_server(extra_args=["--fail"])
        await manager.connect_server(server_id)
        assert (await get_server(server_id)).status == "error"
        async with get_session_factory()() as session:
            server = await session.get(McpServer, server_id)
            assert server is not None
            server.args = [STUB]
            await session.commit()
        await manager.connect_server(server_id)
        assert (await get_server(server_id)).status == "active"


class TestHttpTransport:
    async def test_http_connect_ingests_tools(self, manager: McpManager) -> None:
        import socket

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        stub = FastMCP("http-stub", stateless_http=True)

        def ping_tool(text: str) -> str:
            """Return the text prefixed with pong."""
            return f"pong:{text}"

        stub.add_tool(ping_tool)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                stub.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"
            )
        )
        serve_task = asyncio.create_task(server.serve())
        try:

            async def started() -> bool:
                return server.started

            await wait_for(started)
            async with get_session_factory()() as session:
                record = McpServer(
                    name="http-stub",
                    description="http stub",
                    transport="http",
                    url=f"http://127.0.0.1:{port}/mcp",
                    source="dynamic",
                    status="inactive",
                )
                session.add(record)
                await session.commit()
                server_id = record.id
            await manager.connect_server(server_id)
            assert (await get_server(server_id)).status == "active"
            tools = await tools_of(server_id)
            assert "ping_tool" in tools
            assert tools["ping_tool"].tool_key == "http-stub.ping_tool"
            lc_tools = await manager.get_langchain_tools(server_id, ["ping_tool"])
            result = await lc_tools[0].ainvoke({"text": "hi"})
            assert "pong:hi" in str(result)
            await manager.disconnect_server(server_id)
        finally:
            server.should_exit = True
            await serve_task


class TestListChanged:
    async def test_reconcile_on_notification(self, manager: McpManager) -> None:
        server_id = await make_stub_server()
        await manager.connect_server(server_id)
        assert set(await tools_of(server_id)) >= {"echo", "add"}

        # trigger the server-side toolset mutation; it notifies listChanged
        tools = await manager.get_langchain_tools(server_id, ["mutate_toolset"])
        await tools[0].ainvoke({})

        async def reconciled() -> bool:
            t = await tools_of(server_id)
            return "extra_tool" in t and t["add"].status == "inactive"

        await wait_for(reconciled)
        t = await tools_of(server_id)
        assert t["extra_tool"].status == "active"
        assert t["add"].status == "inactive"  # removed tools marked inactive
        assert t["echo"].status == "active"


class TestInvocation:
    async def test_bound_tools_are_langchain_tools(self, manager: McpManager) -> None:
        server_id = await make_stub_server()
        await manager.connect_server(server_id)
        tools = await manager.get_langchain_tools(server_id, ["echo"])
        assert len(tools) == 1
        result = await tools[0].ainvoke({"text": "hello"})
        assert "echo:hello" in str(result)


class TestHealth:
    async def test_ping_detects_dead_server(self, manager: McpManager) -> None:
        server_id = await make_stub_server()
        await manager.connect_server(server_id)
        assert (await get_server(server_id)).status == "active"

        tools = await manager.get_langchain_tools(server_id, ["die"])
        # the process dies mid-call; that's the point
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tools[0].ainvoke({}), timeout=5)

        await manager.ping_all()
        assert (await get_server(server_id)).status == "error"


class TestStartupAndApi:
    async def test_startup_connects_persisted_servers(self, manager: McpManager) -> None:
        server_id = await make_stub_server()
        await manager.start(connect_timeout=30)

        async def connected() -> bool:
            return (await get_server(server_id)).status == "active"

        await wait_for(connected)

    async def test_post_mcp_server_connects_and_ingests(
        self, manager: McpManager, client: AsyncClient
    ) -> None:
        resp = await client.post(
            f"{API}/mcp-servers",
            json={
                "name": "api-stub",
                "description": "registered via API",
                "transport": "stdio",
                "command": sys.executable,
                "args": [STUB],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "active"
        assert body["tool_count"] >= 2

        listed = (await client.get(f"{API}/tools", params={"q": "echo"})).json()
        assert any(t["tool_key"] == "api-stub.echo" for t in listed)

    async def test_reconnect_endpoint(self, manager: McpManager, client: AsyncClient) -> None:
        server_id = await make_stub_server(extra_args=["--fail"], name="flaky")
        await manager.connect_server(server_id)
        assert (await get_server(server_id)).status == "error"
        async with get_session_factory()() as session:
            server = await session.get(McpServer, server_id)
            assert server is not None
            server.args = [STUB]
            await session.commit()
        resp = await client.post(f"{API}/mcp-servers/{server_id}/reconnect")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"


class TestStdioEnv:
    def test_passthrough_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deployment network env reaches stdio subprocesses; server.env wins."""
        from app.mcp.manager import _stdio_env

        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        monkeypatch.setenv("UV_OFFLINE", "1")
        monkeypatch.setenv("npm_config_offline", "true")
        monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
        env = _stdio_env({"HTTPS_PROXY": "http://per-server:9999", "EXTRA": "x"})
        assert env["HTTPS_PROXY"] == "http://per-server:9999"  # server.env overrides
        assert env["UV_OFFLINE"] == "1"
        assert env["npm_config_offline"] == "true"
        assert env["EXTRA"] == "x"
        assert "SECRET_TOKEN" not in env  # only network-related keys pass through
        assert "PATH" in env  # SDK default environment is preserved
