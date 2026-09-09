"""M29 — delivery channels (spec §18.4): channel adapter registry behind
`ambient_channels`, email/webhook adapters, the per-channel send ledger,
non-blocking failure, and the global ambient SSE stream."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.ambient.channels import (
    email_channel,
    register_channel_adapter,
    register_native_channels,
    registered_channels,
    set_http_client_factory,
    webhook_channel,
)
from app.ambient.deliver import add_delivery, flush_deliveries
from app.config import get_config
from app.db import get_session_factory
from app.models import Delivery
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable(**kv: Any) -> None:
    merged: dict[str, Any] = {
        "ambient_enabled": True,
        "ambient_digest_times": ["23:59"],  # keep digests out of interrupt tests
    }
    merged.update(kv)
    await _set(**merged)


async def _fresh(delivery_id: Any) -> Delivery:
    async with get_session_factory()() as session:
        row = await session.get(Delivery, delivery_id)
        assert row is not None
        return row


# ── adapter registry + ambient_channels validation (spec §18.4) ──────


async def test_in_app_always_registered() -> None:
    assert "in_app" in registered_channels()
    register_native_channels()
    assert {"in_app", "email", "webhook"} <= registered_channels()


async def test_ambient_channels_setting_validation(client: Any) -> None:
    register_native_channels()
    await _set(ambient_channels={"interrupt": ["in_app", "webhook"], "digest": ["email"]})
    with pytest.raises(ValueError, match="unknown channel"):
        await _set(ambient_channels={"digest": ["carrier_pigeon"]})
    with pytest.raises(ValueError, match="mode"):
        await _set(ambient_channels={"smoke_signal": ["in_app"]})
    with pytest.raises(ValueError, match="ambient_channels"):
        await _set(ambient_channels=["email"])


# ── email adapter (spec §18.4: ONE message per batch, env-only) ──────


async def test_email_channel_sends_one_message_per_batch(monkeypatch: Any) -> None:
    sent: list[dict[str, Any]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 0) -> None:
            sent.append({"host": host, "port": port, "messages": []})

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def login(self, user: str, password: str) -> None:
            sent[-1]["login"] = user

        def send_message(self, msg: Any) -> None:
            sent[-1]["messages"].append(msg)

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    cfg = get_config()
    monkeypatch.setattr(cfg, "smtp_host", "mail.local")
    monkeypatch.setattr(cfg, "smtp_port", 2525)
    monkeypatch.setattr(cfg, "smtp_from", "concierge@local")
    monkeypatch.setattr(cfg, "smtp_to", "owner@local")

    rows = [
        Delivery(title="first item", body="alpha", category="ops", tier=2, urgency=3),
        Delivery(title="second item", body="beta", category="ops", tier=2, urgency=2),
    ]
    await email_channel("digest", rows)
    assert len(sent) == 1 and len(sent[0]["messages"]) == 1  # ONE message
    msg = sent[0]["messages"][0]
    assert msg["From"] == "concierge@local" and msg["To"] == "owner@local"
    assert "digest" in msg["Subject"] and "2" in msg["Subject"]
    payload = msg.get_content()
    assert "first item" in payload and "second item" in payload


async def test_email_channel_unconfigured_raises(monkeypatch: Any) -> None:
    cfg = get_config()
    monkeypatch.setattr(cfg, "smtp_host", None)
    with pytest.raises(RuntimeError, match="SMTP"):
        await email_channel("digest", [Delivery(title="x", tier=2, urgency=2)])


# ── webhook adapter (spec §18.4: JSON envelope, gateway-shaped) ──────


async def test_webhook_channel_posts_envelope(monkeypatch: Any) -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200)

    set_http_client_factory(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    cfg = get_config()
    monkeypatch.setattr(cfg, "ambient_webhook_url", "https://gateway.local/push")
    try:
        await webhook_channel(
            "interrupt", [Delivery(title="queue on fire", category="ops", tier=0, urgency=5)]
        )
    finally:
        set_http_client_factory(None)
    assert len(seen) == 1
    url, envelope = seen[0]
    assert url == "https://gateway.local/push"
    assert envelope["kind"] == "ambient_delivery" and envelope["mode"] == "interrupt"
    assert envelope["items"][0]["title"] == "queue on fire"


async def test_webhook_channel_unconfigured_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(get_config(), "ambient_webhook_url", None)
    with pytest.raises(RuntimeError, match="AMBIENT_WEBHOOK_URL"):
        await webhook_channel("interrupt", [Delivery(title="x", tier=0, urgency=5)])


# ── dispatch: routing + send ledger + non-blocking failure ───────────


async def test_flush_dispatches_routed_channel_and_writes_ledger(client: Any) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def chan(mode: str, rows: list[Delivery]) -> None:
        calls.append((mode, [r.title for r in rows]))

    register_channel_adapter("t29chan", chan)
    try:
        await _enable(ambient_channels={"interrupt": ["in_app", "t29chan"]})
        row = await add_delivery(category="ops", tier=0, urgency=5, title="cpu melting")
        out = await flush_deliveries(datetime.now(UTC).replace(hour=12))
        assert out["interrupt"] == 1
        assert calls == [("interrupt", ["cpu melting"])]
        fresh = await _fresh(row.id)
        assert fresh.delivered_at is not None
        ledger = (fresh.external or {}).get("t29chan")
        assert ledger and ledger["ok"] is True and ledger["at"]
    finally:
        register_channel_adapter("t29chan", None)


async def test_channel_failure_never_blocks_in_app(client: Any) -> None:
    async def broken(mode: str, rows: list[Delivery]) -> None:
        raise RuntimeError("gateway down")

    register_channel_adapter("t29broken", broken)
    try:
        await _enable(ambient_channels={"interrupt": ["t29broken"]})
        row = await add_delivery(category="ops", tier=0, urgency=5, title="disk filling")
        out = await flush_deliveries(datetime.now(UTC).replace(hour=12))
        assert out["interrupt"] == 1  # in-app outbox delivered regardless
        fresh = await _fresh(row.id)
        assert fresh.delivered_at is not None
        ledger = (fresh.external or {}).get("t29broken")
        assert ledger and ledger["ok"] is False and "gateway down" in ledger["error"]
    finally:
        register_channel_adapter("t29broken", None)


async def test_no_routing_is_byte_identical(client: Any) -> None:
    await _enable()  # ambient_channels stays {}
    row = await add_delivery(category="ops", tier=0, urgency=5, title="plain interrupt")
    out = await flush_deliveries(datetime.now(UTC).replace(hour=12))
    assert out["interrupt"] == 1
    fresh = await _fresh(row.id)
    # M42 (spec §18.4): `external` now ALSO carries the in_app truth marker
    # when the broadcast reached nobody — which is this test's situation, as
    # no subscriber exists here. The invariant this test actually guards is
    # unchanged and asserted directly: no EXTERNAL channel was contacted.
    assert fresh.delivered_at is not None
    assert set(fresh.external or {}) <= {"in_app"}


async def test_digest_batch_reaches_channel_as_one_call(client: Any) -> None:
    calls: list[tuple[str, int]] = []

    async def chan(mode: str, rows: list[Delivery]) -> None:
        calls.append((mode, len(rows)))

    register_channel_adapter("t29digest", chan)
    try:
        await _enable(
            ambient_channels={"digest": ["in_app", "t29digest"]},
            ambient_digest_times=["00:00"],
        )
        await add_delivery(category="ops", tier=2, urgency=2, title="digest a")
        await add_delivery(category="ops", tier=2, urgency=3, title="digest b")
        out = await flush_deliveries(datetime.now(UTC).replace(hour=12))
        assert out["digest"] == 2
        assert calls == [("digest", 2)]  # the whole batch in ONE adapter call
    finally:
        register_channel_adapter("t29digest", None)


# ── the global ambient SSE stream (spec §18.4) ───────────────────────


async def test_stream_409_when_ambient_dark(client: Any) -> None:
    await _set(ambient_enabled=False)
    resp = await client.get("/api/v1/ambient/stream")
    assert resp.status_code == 409


async def test_stream_broadcasts_delivery_events(client: Any, monkeypatch: Any) -> None:
    """httpx's ASGITransport buffers the full response, so the stream must
    END for the test to read it: shrink the keepalive tick and turn ambient
    dark after the flush — the generator exits on its next dark check."""
    from app.ambient import channels

    monkeypatch.setattr(channels, "STREAM_KEEPALIVE_S", 0.2)
    await _enable()
    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async with client.stream("GET", "/api/v1/ambient/stream") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    received.append(json.loads(line[len("data:") :]))

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    await add_delivery(category="ops", tier=0, urgency=5, title="toast me")
    await flush_deliveries(datetime.now(UTC).replace(hour=12))
    await asyncio.sleep(0.2)
    await _set(ambient_enabled=False)  # dark ⇒ the stream closes itself
    await asyncio.wait_for(task, timeout=10)
    assert received
    event = received[0]
    assert event["mode"] == "interrupt" and event["title"] == "toast me"
    assert event["tier"] == 0 and event["category"] == "ops"
