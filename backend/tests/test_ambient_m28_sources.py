"""M28 — real trigger sources (spec §18.3): parameterized poll/probe
contracts, the three native sources (http_json / rss / mcp_tool), the three
native probes, boot registration, and the watch compiler's config plumbing."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.ambient.sources import (
    http_json_source,
    mcp_tool_source,
    pending_hitl_count,
    register_native_sources,
    rss_source,
    runs_failed_last_hour,
    set_http_client_factory,
    set_mcp_invoker,
    workspace_disk_pct,
)
from app.ambient.triggers import (
    evaluate_state_conditions,
    poll_due_intents,
    poll_source_specs,
    register_poll_source,
    register_state_probe,
    registered_poll_sources,
    registered_state_probes,
    state_probe_specs,
)
from app.db import get_session_factory
from app.models import Run, StandingIntent
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _enable() -> None:
    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_enabled": True})


async def _intent(**kw: Any) -> StandingIntent:
    defaults: dict[str, Any] = {
        "text": f"watch-{uuid4().hex[:8]}",
        "condition_type": "event",
        "status": "active",
    }
    defaults.update(kw)
    async with get_session_factory()() as session:
        intent = StandingIntent(**defaults)
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
        return intent


def _stub_client(handler: Any) -> Any:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── parameterized contracts (spec §18.3) ─────────────────────────────


async def test_poll_source_receives_intent_config() -> None:
    await _enable()
    seen: list[dict[str, Any]] = []

    async def source(
        watermark: str | None, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        seen.append(config)
        return [], watermark

    register_poll_source("m28-cfg", source)
    try:
        await _intent(compiled={"poll": {"source": "m28-cfg", "config": {"url": "x", "n": 3}}})
        await poll_due_intents()
        assert seen == [{"url": "x", "n": 3}]
    finally:
        register_poll_source("m28-cfg", None)


async def test_state_probe_receives_intent_config() -> None:
    await _enable()
    seen: list[dict[str, Any]] = []

    async def probe(config: dict[str, Any]) -> float:
        seen.append(config)
        return 9.0

    register_state_probe("m28-gauge", probe)
    try:
        await _intent(
            condition_type="state",
            compiled={"probe": "m28-gauge", "config": {"path": "/tmp"}, "op": ">=", "value": 5},
        )
        assert await evaluate_state_conditions() == 1
        assert seen == [{"path": "/tmp"}]
    finally:
        register_state_probe("m28-gauge", None)


# ── http_json (spec §18.3) ───────────────────────────────────────────


async def test_http_json_source_items_path_and_watermark() -> None:
    body = {"data": {"alerts": [{"id": "a1", "sev": "high"}, {"id": "a2", "sev": "low"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://feed.example/api"
        return httpx.Response(200, json=body)

    set_http_client_factory(_stub_client(handler))
    try:
        cfg = {"url": "https://feed.example/api", "items_path": "data.alerts"}
        items, wm = await http_json_source(None, cfg)
        assert [i["id"] for i in items] == ["a1", "a2"]
        # second poll with the same feed: nothing new, watermark unchanged
        again, wm2 = await http_json_source(wm, cfg)
        assert again == [] and wm2 == wm
        # a fresh item shows up alone
        body["data"]["alerts"].append({"id": "a3", "sev": "high"})
        fresh, wm3 = await http_json_source(wm, cfg)
        assert [i["id"] for i in fresh] == ["a3"] and wm3 != wm
    finally:
        set_http_client_factory(None)


async def test_http_json_source_hashes_items_without_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"msg": "disk full"}, {"msg": "disk full"}])

    set_http_client_factory(_stub_client(handler))
    try:
        items, wm = await http_json_source(None, {"url": "https://feed.example/x"})
        assert items == [{"msg": "disk full"}]  # identical rows dedupe by hash
        again, _ = await http_json_source(wm, {"url": "https://feed.example/x"})
        assert again == []
    finally:
        set_http_client_factory(None)


async def test_http_json_source_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    set_http_client_factory(_stub_client(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await http_json_source(None, {"url": "https://feed.example/down"})
    finally:
        set_http_client_factory(None)


async def test_broken_source_never_kills_the_tick() -> None:
    await _enable()

    async def bad(
        watermark: str | None, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        raise RuntimeError("boom")

    register_poll_source("m28-bad", bad)
    try:
        await _intent(compiled={"poll": {"source": "m28-bad", "config": {}}})
        assert await poll_due_intents() == 0  # logged, not raised
    finally:
        register_poll_source("m28-bad", None)


# ── rss (spec §18.3: RSS 2.0 + Atom via stdlib XML) ──────────────────

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Ops feed</title>
<item><guid>g-1</guid><title>Deploy finished</title>
  <link>https://ops/1</link><pubDate>Mon, 24 Aug 2026 10:00:00 GMT</pubDate>
  <description>All green</description></item>
<item><guid>g-2</guid><title>Queue depth warning</title>
  <link>https://ops/2</link><pubDate>Mon, 24 Aug 2026 11:00:00 GMT</pubDate>
  <description>Depth 31</description></item>
</channel></rss>"""

_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Releases</title>
<entry><id>tag:rel-9</id><title>v9 shipped</title>
  <link href="https://rel/9"/><updated>2026-08-24T09:00:00Z</updated>
  <summary>notes</summary></entry>
</feed>"""


async def test_rss_source_parses_rss2() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_RSS)

    set_http_client_factory(_stub_client(handler))
    try:
        items, wm = await rss_source(None, {"url": "https://ops/feed.xml"})
        assert [i["id"] for i in items] == ["g-1", "g-2"]
        assert items[1]["title"] == "Queue depth warning"
        assert items[1]["link"] == "https://ops/2"
        again, _ = await rss_source(wm, {"url": "https://ops/feed.xml"})
        assert again == []
    finally:
        set_http_client_factory(None)


async def test_rss_source_parses_atom() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ATOM)

    set_http_client_factory(_stub_client(handler))
    try:
        items, _ = await rss_source(None, {"url": "https://rel/atom.xml"})
        assert len(items) == 1
        assert items[0]["id"] == "tag:rel-9"
        assert items[0]["title"] == "v9 shipped"
        assert items[0]["link"] == "https://rel/9"
    finally:
        set_http_client_factory(None)


# ── mcp_tool (spec §18.3: polls through the MCP manager) ─────────────


async def test_mcp_tool_source_via_manager() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def invoker(server: str, tool: str, args: dict[str, Any]) -> Any:
        calls.append((server, tool, args))
        return json.dumps({"tickets": [{"id": "T-1", "state": "open"}]})

    set_mcp_invoker(invoker)
    try:
        cfg = {
            "server": "helpdesk",
            "tool": "list_tickets",
            "args": {"state": "open"},
            "items_path": "tickets",
        }
        items, wm = await mcp_tool_source(None, cfg)
        assert calls == [("helpdesk", "list_tickets", {"state": "open"})]
        assert items == [{"id": "T-1", "state": "open"}]
        again, _ = await mcp_tool_source(wm, cfg)
        assert again == []
    finally:
        set_mcp_invoker(None)


async def test_mcp_tool_source_wraps_scalar_results() -> None:
    async def invoker(server: str, tool: str, args: dict[str, Any]) -> Any:
        return ["plain-string"]

    set_mcp_invoker(invoker)
    try:
        items, _ = await mcp_tool_source(None, {"server": "s", "tool": "t"})
        assert items == [{"value": "plain-string"}]
    finally:
        set_mcp_invoker(None)


# ── native probes (spec §18.3) ───────────────────────────────────────


async def test_workspace_disk_pct_probe(tmp_path: Any) -> None:
    pct = await workspace_disk_pct({"path": str(tmp_path)})
    assert 0.0 <= pct <= 100.0


async def _run(status: str) -> Run:
    from app.models import Conversation

    async with get_session_factory()() as session:
        conv = Conversation(title="m28")
        session.add(conv)
        await session.flush()
        run = Run(conversation_id=conv.id, chat_message="p", status=status)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def test_pending_hitl_count_probe(client: Any) -> None:
    baseline = await pending_hitl_count({})
    await _run("paused_hitl")
    assert await pending_hitl_count({}) == baseline + 1


async def test_runs_failed_last_hour_probe(client: Any) -> None:
    baseline = await runs_failed_last_hour({})
    await _run("failed")
    stale = await _run("failed")
    async with get_session_factory()() as session:
        row = await session.get(Run, stale.id)
        assert row is not None
        row.started_at = datetime.now(UTC) - timedelta(hours=3)
        row.finished_at = datetime.now(UTC) - timedelta(hours=3)
        await session.commit()
    assert await runs_failed_last_hour({}) == baseline + 1


# ── poll-item filters match ITEM fields (spec §18.3 — live-proof bug) ─


async def test_intent_filters_match_poll_item_fields(client: Any) -> None:
    """The compiler writes poll filters over item fields (`sev`), but poll
    events wrap the item under payload['item'] — decide must unwrap it."""
    from app.ambient.decide import process_event
    from app.ambient.store import emit_event

    await _enable()
    intent = await _intent(
        compiled={
            "poll": {"source": "any", "config": {}},
            "filters": [{"field": "sev", "op": "equals", "value": "HIGH"}],
        }
    )
    hit = await emit_event(
        kind="intent_poll_item",
        source="poll",
        payload={"item": {"id": "AL-9", "sev": "HIGH", "msg": "queue depth climbing"}},
        intent_id=intent.id,
    )
    miss = await emit_event(
        kind="intent_poll_item",
        source="poll",
        payload={"item": {"id": "AL-10", "sev": "low", "msg": "backup done"}},
        intent_id=intent.id,
    )
    assert hit is not None and miss is not None
    verdict_hit, _, decision = await process_event(hit)
    verdict_miss, reason_miss, _ = await process_event(miss)
    assert verdict_hit == "fired" and decision["fired_for"] == "intent"
    assert verdict_miss == "held" and "filters" in reason_miss


# ── boot registration + compiler plumbing (spec §18.3) ───────────────


async def test_register_native_sources_registers_all() -> None:
    register_native_sources()
    assert {"http_json", "rss", "mcp_tool"} <= registered_poll_sources()
    assert {
        "workspace_disk_pct",
        "pending_hitl_count",
        "runs_failed_last_hour",
    } <= registered_state_probes()
    # the compiler prompt needs config shapes for every native entry
    assert "url" in poll_source_specs()["http_json"]
    assert "server" in poll_source_specs()["mcp_tool"]
    assert "path" in state_probe_specs()["workspace_disk_pct"]


async def test_watch_compile_carries_poll_and_probe_config(client: Any) -> None:
    from app.llm import fake as fake_llm
    from app.native.ambient_tools import ambient_watch

    await _enable()
    async with get_session_factory()() as session:
        await update_settings(session, {"default_model": "fake:scripted"})
    register_native_sources()
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "w-m28",
                "name": "WatchCompile",
                "args": {
                    "mode": "poll",
                    "poll_source": "http_json",
                    "poll_config": {"url": "https://feed.example/api", "items_path": "data"},
                    "filters": [{"field": "sev", "op": "equals", "value": "high"}],
                    "cadence_s": 600,
                    "echo": "Poll the alerts feed every 10 minutes for high-severity items.",
                },
            }
        ],
    )
    out = await ambient_watch("tell me when a high-severity alert appears")
    assert out["status"] == "proposed"
    assert out["compiled"]["poll"] == {
        "source": "http_json",
        "config": {"url": "https://feed.example/api", "items_path": "data"},
    }
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "w-m28b",
                "name": "WatchCompile",
                "args": {
                    "mode": "state",
                    "probe": "workspace_disk_pct",
                    "probe_config": {"path": "/workspace"},
                    "op": ">=",
                    "value": 90,
                    "echo": "Alert when the workspace volume passes 90% full.",
                },
            }
        ],
    )
    out2 = await ambient_watch("warn me when the workspace disk is nearly full")
    assert out2["status"] == "proposed"
    assert out2["compiled"] == {
        "probe": "workspace_disk_pct",
        "config": {"path": "/workspace"},
        "op": ">=",
        "value": 90.0,
    }
