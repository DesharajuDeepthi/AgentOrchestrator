"""M41 — ambient pursuit (spec §17.5/§18.4).

`ambient_pursuit` gates the EXTERNAL half of the dispatch on whether the
in-app half reached anyone. The oracle is the SSE subscriber set sampled
at dispatch — the literal audience of the toast just sent — never the
idle timer. Pursuit is a routing modifier: it never re-tiers a row, never
resurrects one quiet hours demoted, and never touches the budget.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.ambient.channels import (
    register_channel_adapter,
    stream_subscriber_count,
    subscribe_stream,
    unsubscribe_stream,
)
from app.ambient.deliver import add_delivery, flush_deliveries
from app.db import get_session_factory
from app.models import Delivery
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio

API = "/api/v1"
NOON = datetime.now(UTC).replace(hour=12)
NIGHT = datetime.now(UTC).replace(hour=23)


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable(**kv: Any) -> None:
    merged: dict[str, Any] = {
        "ambient_enabled": True,
        "ambient_digest_times": ["23:58"],  # keep digests out of interrupt tests
    }
    merged.update(kv)
    await _set(**merged)


async def _fresh(delivery_id: Any) -> Delivery:
    async with get_session_factory()() as session:
        row = await session.get(Delivery, delivery_id)
        assert row is not None
        return row


class _Watcher:
    """Stands in for a browser holding /api/v1/ambient/stream — the SSE
    endpoint registers exactly this way."""

    def __enter__(self) -> "_Watcher":
        self.sub_id, self.queue = subscribe_stream()
        return self

    def __exit__(self, *exc: Any) -> None:
        unsubscribe_stream(self.sub_id)


# ── the setting (spec §3.7) ──────────────────────────────────────────


async def test_default_is_always_byte_identical(client: Any) -> None:
    """Pre-M41 behavior is presence-blind sending; that must be the default."""
    assert (await client.get(f"{API}/settings")).json()["ambient_pursuit"] == "always"


async def test_pursuit_validation(client: Any) -> None:
    for good in ("off", "away", "always"):
        resp = await client.patch(f"{API}/settings", json={"ambient_pursuit": good})
        assert resp.status_code == 200, resp.text
        assert (await client.get(f"{API}/settings")).json()["ambient_pursuit"] == good
    for bad in ("sometimes", "", 1, True, None):
        resp = await client.patch(f"{API}/settings", json={"ambient_pursuit": bad})
        assert resp.status_code == 422, f"{bad!r} accepted: {resp.text}"
    await _set(ambient_pursuit="always")


# ── the presence oracle (spec §18.4) ─────────────────────────────────


async def test_oracle_counts_live_subscribers() -> None:
    base = stream_subscriber_count()
    with _Watcher():
        assert stream_subscriber_count() == base + 1
        with _Watcher():
            assert stream_subscriber_count() == base + 2
        assert stream_subscriber_count() == base + 1
    assert stream_subscriber_count() == base


# ── the tri-state matrix, driven through the REAL flush ──────────────


@pytest.mark.parametrize(
    ("pursuit", "watching", "external_fires"),
    [
        ("always", True, True),  # pre-M41 behavior: presence-blind
        ("always", False, True),
        ("away", True, False),  # the toast landed — do not also email
        ("away", False, True),  # nobody saw it — pursue
        ("off", True, False),  # in-app only, always
        ("off", False, False),
    ],
)
async def test_pursuit_matrix(
    client: Any, pursuit: str, watching: bool, external_fires: bool
) -> None:
    seen: list[str] = []

    async def chan(mode: str, rows: list[Delivery]) -> None:
        seen.append(mode)

    # the adapter must exist before the routing naming it is validated
    register_channel_adapter("t41chan", chan)
    try:
        await _enable(
            ambient_pursuit=pursuit, ambient_channels={"interrupt": ["in_app", "t41chan"]}
        )
        delivery = await add_delivery(
            category="ops", tier=0, urgency=5, title=f"m41 {pursuit} watching={watching}"
        )
        if watching:  # the subscriber must be live at DISPATCH time
            with _Watcher():
                out = await flush_deliveries(NOON)
        else:
            out = await flush_deliveries(NOON)
        row = await _fresh(delivery.id)
    finally:
        register_channel_adapter("t41chan", None)

    assert out["interrupt"] == 1, out  # the in-app decision is never altered
    assert (seen == ["interrupt"]) is external_fires, f"{pursuit}/{watching}: {seen}"
    # the ledger is the audit trail: a suppressed escalation is an ABSENT
    # entry, never a silent one
    ledger = (row.external or {}).get("t41chan")
    if external_fires:
        assert ledger and ledger["ok"] is True
    else:
        assert ledger is None
    # in every branch the in-app decision is untouched
    assert row.delivered_at is not None and row.tier == 0 and row.channel == "interrupt"


# ── subordination (spec §17.5) ───────────────────────────────────────


async def test_quiet_hours_beat_pursuit(client: Any) -> None:
    """Pursuit escalates the channel, never the hour: inside quiet hours a
    tier-0 is demoted and NOTHING is dispatched — no toast, no external."""
    seen: list[str] = []

    async def chan(mode: str, rows: list[Delivery]) -> None:
        seen.append(mode)

    register_channel_adapter("t41quiet", chan)
    try:
        await _enable(
            ambient_pursuit="away",
            ambient_quiet_hours=["22:00", "07:00"],
            ambient_channels={"interrupt": ["t41quiet"]},
        )
        row = await add_delivery(category="ops", tier=0, urgency=5, title="3am failure")
        out = await flush_deliveries(NIGHT)  # nobody watching, deepest pursuit case
        assert out["interrupt"] == 0 and out["demoted"] == 1
        assert seen == []  # no escalation of a row that never delivered
        fresh = await _fresh(row.id)
        assert fresh.tier == 2 and fresh.delivered_at is None and fresh.external is None
    finally:
        register_channel_adapter("t41quiet", None)


async def test_pursuit_does_not_touch_the_budget(client: Any) -> None:
    """The budget is spent by the tier decision, not by pursuit: the same
    interrupts are delivered whether or not anyone is watching."""

    async def chan(mode: str, rows: list[Delivery]) -> None:
        return None

    register_channel_adapter("t41budget", chan)
    try:
        await _enable(
            ambient_pursuit="away",
            ambient_notification_budget_per_day=2,
            ambient_channels={"interrupt": ["t41budget"]},
        )
        with _Watcher():  # watching ⇒ no external sends at all
            for i in range(3):
                await add_delivery(category="ops", tier=0, urgency=5, title=f"budget probe {i}")
            out = await flush_deliveries(NOON)
        assert out["interrupt"] == 2 and out["demoted"] == 1  # budget still governs
    finally:
        register_channel_adapter("t41budget", None)
        await _set(ambient_notification_budget_per_day=3)


async def test_in_app_routing_entry_is_not_an_external_channel(client: Any) -> None:
    """`in_app` names the outbox itself — pursuit must not treat it as an
    escalation target, so an in_app-only routing sends nothing external."""
    await _enable(ambient_pursuit="away", ambient_channels={"interrupt": ["in_app"]})
    row = await add_delivery(category="ops", tier=0, urgency=5, title="in-app only")
    out = await flush_deliveries(NOON)  # nobody watching
    assert out["interrupt"] == 1
    fresh = await _fresh(row.id)
    # M42: the in_app truth marker is expected here (nobody was watching);
    # what must NOT appear is an external adapter entry for `in_app`
    assert set(fresh.external or {}) <= {"in_app"}
    assert (fresh.external or {}).get("in_app", {}).get("ok") is False
