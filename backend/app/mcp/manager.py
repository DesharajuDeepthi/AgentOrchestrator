"""MCP connection manager (spec §5).

Singleton service owning one client session per active mcp_server record.
Each connection lives in its own asyncio task that holds the client context
open; tools/list results are upserted into the tools registry with source
inherited from the server; listChanged notifications trigger reconciliation;
a ping loop (interval from settings, live) flips failing servers to 'error'.
DB is the source of truth — startup loads and connects every non-deleted
server, so dynamic records survive restarts.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import mcp.types as mcp_types
import structlog
from langchain_core.tools import BaseTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select

from app.db import get_session_factory
from app.models import McpServer, Tool

logger = structlog.get_logger("mcp.manager")

CONNECT_TIMEOUT_S = 25.0
PING_TIMEOUT_S = 5.0

# The MCP SDK spawns stdio servers with a minimal default environment, which
# strips deployment-level network config — behind a proxy, uvx/npx launchers
# can't reach their registries. Pass these through; server.env still wins.
_STDIO_ENV_PASSTHROUGH = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS",
)
_STDIO_ENV_PREFIXES = ("UV_", "npm_config_")


def _stdio_env(server_env: dict[str, Any] | None) -> dict[str, str]:
    import os

    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    for key, value in os.environ.items():
        if key in _STDIO_ENV_PASSTHROUGH or key.startswith(_STDIO_ENV_PREFIXES):
            env[key] = value
    env.update({str(k): str(v) for k, v in (server_env or {}).items()})
    return env


class _Connection:
    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.error: BaseException | None = None


class McpManager:
    def __init__(self) -> None:
        self._conns: dict[UUID, _Connection] = {}
        self._health_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self, connect_timeout: float = CONNECT_TIMEOUT_S) -> None:
        """Connect every non-deleted server (spec §5 startup) + health loop."""
        async with get_session_factory()() as db:
            server_ids = list(
                (
                    await db.execute(select(McpServer.id).where(McpServer.deleted_at.is_(None)))
                ).scalars()
            )
        await asyncio.gather(
            *(self.connect_server(sid, timeout_s=connect_timeout) for sid in server_ids),
            return_exceptions=True,
        )
        if self._health_task is None:
            self._health_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        for server_id in list(self._conns):
            await self._teardown(server_id)

    # ── connection ───────────────────────────────────────────────

    async def connect_server(self, server_id: UUID, timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        """(Re)connect, ingest tools, and record status on the server row."""
        await self._teardown(server_id)
        async with get_session_factory()() as db:
            server = await db.get(McpServer, server_id)
            if server is None or server.deleted_at is not None:
                return
        conn = _Connection()
        self._conns[server_id] = conn
        conn.task = asyncio.create_task(self._run_connection(server, conn))
        try:
            await asyncio.wait_for(conn.ready.wait(), timeout=timeout_s)
            if conn.error is not None:
                raise conn.error
            await self._ingest(server_id)
        except BaseException as exc:  # noqa: BLE001 - connection failure path
            await self._teardown(server_id)
            await self._record_status(server_id, "error", error=_describe(exc))
            logger.warning("mcp_connect_failed", server_id=str(server_id), error=_describe(exc))
            return
        await self._record_status(server_id, "active", connected=True)
        logger.info("mcp_connected", server_id=str(server_id), server_name=server.name)

    async def _run_connection(self, server: McpServer, conn: _Connection) -> None:
        try:
            if server.transport == "stdio":
                params = StdioServerParameters(
                    command=server.command or "",
                    args=[str(a) for a in (server.args or [])],
                    env=_stdio_env(server.env),
                )
                client_ctx: Any = stdio_client(params)
            else:
                headers = {str(k): str(v) for k, v in (server.headers or {}).items()}
                client_ctx = streamablehttp_client(server.url or "", headers=headers or None)
            async with client_ctx as streams:
                read, write = streams[0], streams[1]

                async def on_message(message: Any) -> None:
                    self._handle_notification(server.id, message)

                async with ClientSession(read, write, message_handler=on_message) as session:
                    await session.initialize()
                    conn.session = session
                    conn.ready.set()
                    await conn.stop_event.wait()
        except BaseException as exc:  # noqa: BLE001 - report to connect_server
            conn.error = exc
        finally:
            conn.session = None
            conn.ready.set()

    async def _teardown(self, server_id: UUID) -> None:
        conn = self._conns.pop(server_id, None)
        if conn is None:
            return
        conn.stop_event.set()
        if conn.task is not None:
            conn.task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(conn.task, timeout=5)

    async def disconnect_server(self, server_id: UUID) -> None:
        await self._teardown(server_id)

    # ── ingest / reconcile (spec §5 register + listChanged) ─────

    async def refresh_tools(self, server_id: UUID) -> None:
        await self._ingest(server_id)

    async def _ingest(self, server_id: UUID) -> None:
        conn = self._conns.get(server_id)
        if conn is None or conn.session is None:
            raise RuntimeError(f"server {server_id} is not connected")
        result = await conn.session.list_tools()
        async with get_session_factory()() as db:
            server = await db.get(McpServer, server_id)
            if server is None:
                return
            existing = {
                t.tool_name: t
                for t in (
                    await db.execute(select(Tool).where(Tool.mcp_server_id == server_id))
                ).scalars()
            }
            taken_keys = set((await db.execute(select(Tool.tool_key))).scalars())
            seen: set[str] = set()
            for spec in result.tools:
                seen.add(spec.name)
                row = existing.get(spec.name)
                if row is None:
                    key = f"{server.name}.{spec.name}"
                    if key in taken_keys:  # collision-safe (spec §3.2)
                        key = f"{key}-{uuid4().hex[:6]}"
                    taken_keys.add(key)
                    db.add(
                        Tool(
                            name=spec.name,
                            description=spec.description or "",
                            kind="mcp",
                            source=server.source,  # inherited (spec §5)
                            status="active",
                            mcp_server_id=server_id,
                            tool_name=spec.name,
                            tool_key=key,
                            input_schema=spec.inputSchema,
                        )
                    )
                else:
                    row.description = spec.description or ""
                    row.input_schema = spec.inputSchema
                    row.status = "active"
                    row.deleted_at = None
            for name, row in existing.items():
                if name not in seen and row.status != "inactive":
                    row.status = "inactive"  # removed tools marked inactive
            await db.commit()
        from app.registry_cache import get_cache

        await get_cache().invalidate("tools")
        logger.info(
            "mcp_tools_ingested",
            server_id=str(server_id),
            tool_count=len(result.tools),
        )

    def _handle_notification(self, server_id: UUID, message: Any) -> None:
        root = getattr(message, "root", None)
        if isinstance(root, mcp_types.ToolListChangedNotification):
            logger.info("mcp_list_changed", server_id=str(server_id))
            task = asyncio.create_task(self._safe_refresh(server_id))
            task.add_done_callback(lambda t: t.exception())

    async def _safe_refresh(self, server_id: UUID) -> None:
        with contextlib.suppress(Exception):
            await self._ingest(server_id)

    # ── health (spec §5) ─────────────────────────────────────────

    async def ping_all(self) -> None:
        for server_id, conn in list(self._conns.items()):
            session = conn.session
            ok = False
            if session is not None:
                try:
                    await asyncio.wait_for(session.send_ping(), timeout=PING_TIMEOUT_S)
                    ok = True
                except BaseException:  # noqa: BLE001
                    ok = False
            if not ok:
                await self._teardown(server_id)
                await self._record_status(server_id, "error", error="health ping failed")
                logger.warning("mcp_ping_failed", server_id=str(server_id))

    async def _health_loop(self) -> None:
        from app.settings_store import get_setting

        while True:
            async with get_session_factory()() as db:
                interval = int(await get_setting(db, "mcp_health_interval_s"))
            await asyncio.sleep(max(interval, 1))
            await self.ping_all()

    # ── invocation (spec §5): bound tools as LangChain tools ────

    async def get_langchain_tools(self, server_id: UUID, tool_names: list[str]) -> list[BaseTool]:
        conn = self._conns.get(server_id)
        if conn is None or conn.session is None:
            raise RuntimeError(f"MCP server {server_id} is not connected")
        from langchain_mcp_adapters.tools import load_mcp_tools

        tools = await load_mcp_tools(conn.session)
        wanted = set(tool_names)
        return [t for t in tools if t.name in wanted]

    def is_connected(self, server_id: UUID) -> bool:
        conn = self._conns.get(server_id)
        return conn is not None and conn.session is not None

    # ── helpers ──────────────────────────────────────────────────

    async def _record_status(
        self, server_id: UUID, status: str, error: str | None = None, connected: bool = False
    ) -> None:
        async with get_session_factory()() as db:
            server = await db.get(McpServer, server_id)
            if server is None:
                return
            server.status = status
            server.last_error = error
            if connected:
                server.last_connected_at = datetime.now(UTC)
            await db.commit()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "connection timed out"
    if isinstance(exc, BaseExceptionGroup):
        parts = [_describe(e) for e in exc.exceptions]
        return "; ".join(dict.fromkeys(parts))
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


_manager: McpManager | None = None


def get_manager() -> McpManager | None:
    return _manager


def set_manager(manager: McpManager | None) -> None:
    global _manager
    _manager = manager
