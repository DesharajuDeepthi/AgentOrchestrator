"""Delivery channels (spec §18.4 — milestone M29).

A channel adapter registry behind the `ambient_channels` setting (per-tier
routing, validated against registered adapters). `in_app` is the outbox
itself and always exists; `email` renders a batch as ONE SMTP message
(SMTP_* env-only — no secrets in the DB); `webhook` POSTs one JSON envelope
to AMBIENT_WEBHOOK_URL (the SMS/push-gateway shape). Every external send is
recorded per channel on the delivery row (`external` jsonb); a channel
failure logs and never blocks the in-app outbox.

The same dispatch hook feeds the global ambient SSE stream: one event per
delivered row, consumed by `/api/v1/ambient/stream` subscribers (the
in-app toast). With `ambient_channels` empty the dispatch is a no-op and
delivery behavior stays byte-identical to M23–M25.
"""

import asyncio
import json
import smtplib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx
import structlog

from app.db import get_session_factory
from app.models import Delivery

logger = structlog.get_logger("ambient")

DELIVERY_MODES = {"interrupt", "notify", "digest"}

# channel adapter contract: (mode, delivered rows) -> None; raise = failure
ChannelAdapter = Callable[[str, list[Delivery]], Awaitable[None]]
_ADAPTERS: dict[str, ChannelAdapter] = {}

_http_client_factory: Callable[[], httpx.AsyncClient] | None = None


def set_http_client_factory(fn: Callable[[], httpx.AsyncClient] | None) -> None:
    global _http_client_factory
    _http_client_factory = fn


def _client() -> httpx.AsyncClient:
    if _http_client_factory is not None:
        return _http_client_factory()
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False)


def register_channel_adapter(name: str, fn: ChannelAdapter | None) -> None:
    if fn is None:
        _ADAPTERS.pop(name, None)
    else:
        _ADAPTERS[name] = fn


def registered_channels() -> set[str]:
    """`in_app` is the outbox itself — always present, never an adapter."""
    return {"in_app"} | set(_ADAPTERS)


# ── native adapters (spec §18.4) ─────────────────────────────────────


def _item_lines(rows: list[Delivery]) -> str:
    lines = []
    for row in rows:
        lines.append(f"• [{row.category} · urgency {row.urgency}] {row.title}")
        if row.body:
            lines.append(f"  {row.body}")
    return "\n".join(lines)


async def email_channel(mode: str, rows: list[Delivery]) -> None:
    """ONE message per batch over SMTP; SMTP_HOST/PORT/USER/PASSWORD/FROM/TO
    are env-only (spec §13) — an unset host is a config error, not silence."""
    from app.config import get_config

    cfg = get_config()
    if not cfg.smtp_host or not cfg.smtp_from or not cfg.smtp_to:
        raise RuntimeError("SMTP is not configured (SMTP_HOST/SMTP_FROM/SMTP_TO)")
    msg = EmailMessage()
    msg["From"] = cfg.smtp_from
    msg["To"] = cfg.smtp_to
    msg["Subject"] = f"[concierge] ambient {mode}: {len(rows)} item(s)"
    msg.set_content(_item_lines(rows) + "\n\n— concierge ambient delivery\n")

    def _send() -> None:
        with smtplib.SMTP(cfg.smtp_host or "", cfg.smtp_port, timeout=15) as smtp:
            if cfg.smtp_user and cfg.smtp_password:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)

    await asyncio.to_thread(_send)


async def webhook_channel(mode: str, rows: list[Delivery]) -> None:
    """One JSON envelope per batch to AMBIENT_WEBHOOK_URL — the generic
    SMS/push-gateway shape; any provider bridge terminates there."""
    from app.config import get_config

    url = get_config().ambient_webhook_url
    if not url:
        raise RuntimeError("webhook channel is not configured (AMBIENT_WEBHOOK_URL)")
    envelope = {
        "kind": "ambient_delivery",
        "mode": mode,
        "items": [
            {
                "id": str(row.id),
                "category": row.category,
                "tier": row.tier,
                "urgency": row.urgency,
                "title": row.title,
                "body": row.body,
            }
            for row in rows
        ],
    }
    async with _client() as client:
        resp = await client.post(url, json=envelope)
        resp.raise_for_status()


def register_native_channels() -> None:
    """Idempotent; called from the app lifespan."""
    register_channel_adapter("email", email_channel)
    register_channel_adapter("webhook", webhook_channel)


# ── the SSE broadcast hub (spec §18.4 toast) ─────────────────────────

_subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}
_next_sub = 0
# keepalive cadence for the SSE stream; each tick also re-checks
# ambient_enabled so a stream closes soon after ambient goes dark
STREAM_KEEPALIVE_S = 15.0


def subscribe_stream() -> tuple[int, asyncio.Queue[dict[str, Any]]]:
    global _next_sub
    _next_sub += 1
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    _subscribers[_next_sub] = queue
    return _next_sub, queue


def unsubscribe_stream(sub_id: int) -> None:
    _subscribers.pop(sub_id, None)


def stream_subscriber_count() -> int:
    """The §18.4 pursuit presence oracle: how many subscribers `_publish`
    would fan out to right now. Not an estimate of presence — it IS the
    audience of the toast, which is exactly the question pursuit asks.
    Per-process by construction, and correct that way: under §18.9 a tick
    on this replica can only ever toast this replica's subscribers."""
    return len(_subscribers)


def _publish(mode: str, rows: list[Delivery]) -> None:
    if not _subscribers:
        return
    now = datetime.now(UTC).isoformat()
    for row in rows:
        event = {
            "id": str(row.id),
            "mode": mode,
            "tier": row.tier,
            "urgency": row.urgency,
            "category": row.category,
            "title": row.title,
            "at": now,
        }
        for queue in list(_subscribers.values()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # a stalled consumer never blocks the tick
                continue


def sse_line(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


# ── the dispatch hook (called by every flush path) ───────────────────


async def _record_send(rows: list[Delivery], channel: str, entry: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        for row in rows:
            fresh = await session.get(Delivery, row.id)
            if fresh is None:
                continue
            fresh.external = {**(fresh.external or {}), channel: entry}
        await session.commit()


# the real-time modes: these are the ones a human was meant to see AS THEY
# HAPPENED. A digest reaching an empty room is its normal condition, not a
# failure, so it is never marked unseen (spec §18.4, M42)
_REALTIME_MODES = {"interrupt", "notify"}


async def _record_in_app_outcome(mode: str, rows: list[Delivery], watchers: int) -> None:
    """M42: record the truth when the in-app broadcast reached nobody.

    Written ONLY on the lossy path — the happy path leaves `external` null,
    so byte-identity at defaults is preserved (spec §18.4)."""
    if mode not in _REALTIME_MODES or watchers > 0:
        return
    entry = {
        "ok": False,
        "error": "no subscriber",
        "at": datetime.now(UTC).isoformat(),
    }
    await _record_send(rows, "in_app", entry)
    logger.info(
        "ambient_delivered_unseen",
        tier="ambient",
        kind="deliver",
        mode=mode,
        count=len(rows),
        delivery_ids=[str(r.id) for r in rows],
    )


def _pursue(pursuit: str, watchers: int) -> bool:
    """Should the external channels fire for a batch the in-app hub just
    reached `watchers` subscribers with? (spec §17.5/§18.4, M41)

    - `always` — presence-blind, the pre-M41 behavior and the default
    - `away`   — only when the toast reached nobody
    - `off`    — never; in-app is the whole delivery
    """
    if pursuit == "off":
        return False
    if pursuit == "away":
        return watchers == 0
    return True  # 'always', and any unknown value fails safe to it


async def dispatch_delivered(mode: str, rows: list[Delivery]) -> None:
    """Fan a just-delivered batch out: SSE stream always, external channels
    per the `ambient_channels` routing. Failures are ledgered, logged, and
    never raised — the in-app outbox is already the source of truth."""
    if not rows:
        return
    # sample the oracle BEFORE publishing: this count is precisely the
    # audience `_publish` is about to reach (spec §18.4, M41)
    watchers = stream_subscriber_count()
    _publish(mode, rows)
    from app.registry_cache import get_cache

    routing = dict(await get_cache().setting("ambient_channels") or {})
    names = [str(n) for n in (routing.get(mode) or []) if n != "in_app"]
    # §17.5 pursuit: a routing modifier over the EXTERNAL half only — the
    # in-app outbox row and its toast above are already decided and sent
    await _record_in_app_outcome(mode, rows, watchers)
    pursuit = str(await get_cache().setting("ambient_pursuit") or "always")
    if names and not _pursue(pursuit, watchers):
        logger.info(
            "ambient_pursuit_held",
            tier="ambient",
            kind="deliver",
            mode=mode,
            pursuit=pursuit,
            watchers=watchers,
            channels=names,
        )
        return
    for name in names:
        adapter = _ADAPTERS.get(name)
        entry: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
        if adapter is None:
            entry.update(ok=False, error=f"channel {name!r} is not registered")
        else:
            try:
                await adapter(mode, rows)
                entry.update(ok=True, error=None)
            except Exception as exc:  # noqa: BLE001 — never blocks the outbox
                entry.update(ok=False, error=str(exc)[:500])
        if not entry["ok"]:
            logger.warning(
                "ambient_channel_failed",
                tier="ambient",
                kind="deliver",
                channel=name,
                error=entry.get("error"),
            )
        await _record_send(rows, name, entry)
        from app import obs

        obs.AMBIENT_OPS.labels(
            kind="channel", status=f"{name}_{'ok' if entry['ok'] else 'error'}"
        ).inc()
