"""Native poll sources + state probes (spec §18.3 — milestone M28).

Sources implement the parameterized contract `fn(watermark, config) ->
(items, watermark)`; probes implement `fn(config) -> float`. All are
registered at boot via `register_native_sources()` so the watch compiler
can list them with their config shapes.

Watermark: the sources here store a bounded JSON list of recently-seen item
keys (id / hash) — the "last id/hash" of §18.3 generalized so unordered
feeds and mid-list insertions still dedupe correctly. Any non-JSON legacy
watermark reads as empty (everything is new once).

Transports are injectable for tests: `set_http_client_factory` swaps the
httpx client used by http_json/rss; `set_mcp_invoker` swaps the MCP call
used by mcp_tool (the default resolves the server by name and invokes the
tool through the MCP manager).
"""

import hashlib
import json
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from xml.etree import ElementTree

import httpx
import structlog
from sqlalchemy import func, select

from app.db import get_session_factory
from app.models import Run

logger = structlog.get_logger("ambient")

_SEEN_CAP = 100  # keys remembered per watch — bounds the watermark row

# ── injectable transports ────────────────────────────────────────────

_http_client_factory: Callable[[], httpx.AsyncClient] | None = None
_mcp_invoker: Callable[[str, str, dict[str, Any]], Awaitable[Any]] | None = None


def set_http_client_factory(fn: Callable[[], httpx.AsyncClient] | None) -> None:
    global _http_client_factory
    _http_client_factory = fn


def set_mcp_invoker(fn: Callable[[str, str, dict[str, Any]], Awaitable[Any]] | None) -> None:
    global _mcp_invoker
    _mcp_invoker = fn


def _client() -> httpx.AsyncClient:
    if _http_client_factory is not None:
        return _http_client_factory()
    return httpx.AsyncClient(timeout=20.0, follow_redirects=True)


# ── shared helpers ───────────────────────────────────────────────────


def _seen_keys(watermark: str | None) -> list[str]:
    if not watermark:
        return []
    try:
        parsed = json.loads(watermark)
    except (ValueError, TypeError):
        return []
    return [str(k) for k in parsed] if isinstance(parsed, list) else []


def _item_key(item: dict[str, Any], id_field: str) -> str:
    if item.get(id_field) not in (None, ""):
        return str(item[id_field])
    digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


def _dedupe(
    items: list[dict[str, Any]], watermark: str | None, id_field: str = "id"
) -> tuple[list[dict[str, Any]], str | None]:
    """Keep items whose key is unseen; roll the seen-list forward."""
    seen = _seen_keys(watermark)
    seen_set = set(seen)
    fresh: list[dict[str, Any]] = []
    for item in items:
        key = _item_key(item, id_field)
        if key in seen_set:
            continue
        seen_set.add(key)
        seen.append(key)
        fresh.append(item)
    if not fresh:
        return [], watermark
    return fresh, json.dumps(seen[-_SEEN_CAP:])


def _walk_path(data: Any, path: str | None) -> Any:
    for part in [p for p in (path or "").split(".") if p]:
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _as_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError(f"items path did not resolve to a list (got {type(data).__name__})")
    return [it if isinstance(it, dict) else {"value": it} for it in data]


# ── native poll sources (spec §18.3) ─────────────────────────────────


async def http_json_source(
    watermark: str | None, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """Poll a JSON endpoint: `{url, items_path?, id_field?}`."""
    url = str(config.get("url") or "")
    if not url:
        raise ValueError("http_json needs config.url")
    async with _client() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    items = _as_items(_walk_path(payload, config.get("items_path")))
    return _dedupe(items, watermark, str(config.get("id_field") or "id"))


def _first_text(elem: ElementTree.Element, *tags: str) -> str:
    for tag in tags:
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return ""


_ATOM_NS = "{http://www.w3.org/2005/Atom}"


async def rss_source(
    watermark: str | None, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """Poll an RSS 2.0 or Atom feed via stdlib XML: `{url}`."""
    url = str(config.get("url") or "")
    if not url:
        raise ValueError("rss needs config.url")
    async with _client() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text
    root = ElementTree.fromstring(text)
    items: list[dict[str, Any]] = []
    for entry in root.iter("item"):  # RSS 2.0
        link = _first_text(entry, "link")
        items.append(
            {
                "id": _first_text(entry, "guid") or link,
                "title": _first_text(entry, "title"),
                "link": link,
                "published": _first_text(entry, "pubDate"),
                "summary": _first_text(entry, "description"),
            }
        )
    for entry in root.iter(f"{_ATOM_NS}entry"):  # Atom
        link_el = entry.find(f"{_ATOM_NS}link")
        link = link_el.get("href", "") if link_el is not None else ""
        items.append(
            {
                "id": _first_text(entry, f"{_ATOM_NS}id") or link,
                "title": _first_text(entry, f"{_ATOM_NS}title"),
                "link": link,
                "published": _first_text(entry, f"{_ATOM_NS}updated"),
                "summary": _first_text(entry, f"{_ATOM_NS}summary"),
            }
        )
    return _dedupe(items, watermark)


async def _default_mcp_invoke(server_name: str, tool: str, args: dict[str, Any]) -> Any:
    """Resolve the server by name and call the tool through the MCP manager
    — the sanctioned invocation path (spec §5), no raw sessions here."""
    from app.mcp.manager import get_manager
    from app.models import McpServer

    manager = get_manager()
    if manager is None:
        raise RuntimeError("MCP manager is not running")
    async with get_session_factory()() as session:
        server = (
            await session.execute(select(McpServer).where(McpServer.name == server_name))
        ).scalar_one_or_none()
    if server is None:
        raise ValueError(f"no MCP server named {server_name!r}")
    tools = await manager.get_langchain_tools(server.id, [tool])
    if not tools:
        raise ValueError(f"server {server_name!r} exposes no tool named {tool!r}")
    return await tools[0].ainvoke(args)


async def mcp_tool_source(
    watermark: str | None, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """Poll an MCP tool: `{server, tool, args?, items_path?, id_field?}` —
    the POC form of "MCP subscriptions where available" (§18.3)."""
    server = str(config.get("server") or "")
    tool = str(config.get("tool") or "")
    if not server or not tool:
        raise ValueError("mcp_tool needs config.server and config.tool")
    invoke = _mcp_invoker or _default_mcp_invoke
    result = await invoke(server, tool, dict(config.get("args") or {}))
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {"value": result}
    items = _as_items(_walk_path(result, config.get("items_path")))
    return _dedupe(items, watermark, str(config.get("id_field") or "id"))


# ── native state probes (spec §18.3) ─────────────────────────────────


async def workspace_disk_pct(config: dict[str, Any]) -> float:
    """Percent of the workspace volume in use: `{path?}`."""
    from app.config import get_config

    path = str(config.get("path") or get_config().workspace_dir)
    usage = shutil.disk_usage(path)
    return (usage.used / usage.total) * 100.0 if usage.total else 0.0


async def pending_hitl_count(config: dict[str, Any]) -> float:
    """Runs currently paused on a human-input gate."""
    _ = config
    async with get_session_factory()() as session:
        count = (
            await session.execute(select(func.count()).where(Run.status == "paused_hitl"))
        ).scalar_one()
    return float(count)


async def runs_failed_last_hour(config: dict[str, Any]) -> float:
    """Runs that failed within the last hour."""
    _ = config
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                select(func.count()).where(
                    Run.status == "failed",
                    func.coalesce(Run.finished_at, Run.started_at) >= cutoff,
                )
            )
        ).scalar_one()
    return float(count)


# ── boot registration (spec §18.3) ───────────────────────────────────


def register_native_sources() -> None:
    """Idempotent; called from the app lifespan so every boot exposes the
    native registries to the tick and the watch compiler."""
    from app.ambient.triggers import register_poll_source, register_state_probe

    register_poll_source(
        "http_json",
        http_json_source,
        config_shape="{url, items_path?, id_field? (default 'id')}",
    )
    register_poll_source("rss", rss_source, config_shape="{url}")
    register_poll_source(
        "mcp_tool",
        mcp_tool_source,
        config_shape="{server, tool, args?, items_path?, id_field? (default 'id')}",
    )
    register_state_probe(
        "workspace_disk_pct",
        workspace_disk_pct,
        config_shape="{path? (default: the workspace volume)}",
    )
    register_state_probe("pending_hitl_count", pending_hitl_count, config_shape="{}")
    register_state_probe("runs_failed_last_hour", runs_failed_last_hour, config_shape="{}")
    logger.info("ambient_native_sources_registered", tier="ambient", kind="boot")
