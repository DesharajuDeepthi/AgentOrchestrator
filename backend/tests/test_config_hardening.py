"""M40 config-hardening contract tests (spec §3.7 additions).

Every promoted key: validation bounds reject out-of-range writes, good
writes read back, and the consumer actually honors the live value. The
defaults equal the constants they replaced, so the rest of the suite is
the byte-identity net.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.a2a.fence import fence_remote_output, live_fence_cap
from app.auth import _rate_limited
from app.db import get_session_factory
from app.overlap import OverlapVerdict, _threshold, _to_out
from app.settings_store import update_settings

API = "/api/v1"

BOUNDS = [
    # key, bad-low, bad-type, good
    ("ambient_tick_interval_s", 14, "x", 30),
    ("rate_limit_burst", 0, "x", 50),
    ("rate_limit_per_s", 0, 1.5, 5),
    ("overlap_threshold_percent", -1, "x", 0),
    ("run_stall_after_s", 59, "x", 120),
    ("agentic_recursion_limit", 9, "x", 250),
    ("a2a_http_timeout_s", 0, "x", 30),
    ("a2a_fence_max_chars", 499, "x", 500),
]


class TestValidationBounds:
    async def test_bad_values_reject_good_values_stick(self, client: AsyncClient) -> None:
        for key, bad_low, bad_type, good in BOUNDS:
            for bad in (bad_low, bad_type):
                resp = await client.patch(f"{API}/settings", json={key: bad})
                assert resp.status_code == 422, f"{key}={bad!r} accepted: {resp.text}"
            resp = await client.patch(f"{API}/settings", json={key: good})
            assert resp.status_code == 200, f"{key}={good!r} rejected: {resp.text}"
            assert (await client.get(f"{API}/settings")).json()[key] == good

    async def test_overlap_threshold_upper_bound(self, client: AsyncClient) -> None:
        resp = await client.patch(f"{API}/settings", json={"overlap_threshold_percent": 101})
        assert resp.status_code == 422
        resp = await client.patch(f"{API}/settings", json={"overlap_threshold_percent": 100})
        assert resp.status_code == 200

    async def test_agentic_recursion_upper_bound(self, client: AsyncClient) -> None:
        resp = await client.patch(f"{API}/settings", json={"agentic_recursion_limit": 501})
        assert resp.status_code == 422


class TestOverlapThresholdWiring:
    def test_gate_uses_passed_threshold(self) -> None:
        verdict = OverlapVerdict(overlap_percent=50, reasoning="test")
        assert _to_out(verdict, 40).overlap is True
        assert _to_out(verdict, 70).overlap is False
        assert _to_out(verdict, 50).overlap is True  # >= semantics

    async def test_threshold_reads_live_setting(self, client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(session, {"overlap_threshold_percent": 40})
        assert await _threshold() == 40
        async with get_session_factory()() as session:
            await update_settings(session, {"overlap_threshold_percent": 70})
        assert await _threshold() == 70


class TestRateLimitWiring:
    def test_bucket_honors_shape_params(self) -> None:
        key = "test-user-m40"
        from app import auth

        auth._buckets.pop(key, None)
        assert _rate_limited(key, burst=2.0, per_s=0.0001) is False
        assert _rate_limited(key, burst=2.0, per_s=0.0001) is False
        assert _rate_limited(key, burst=2.0, per_s=0.0001) is True  # burst spent
        auth._buckets.pop(key, None)


class TestFenceCapWiring:
    def test_explicit_cap_truncates(self) -> None:
        fenced = fence_remote_output("x" * 5000, agent_name="a", max_chars=600)
        assert "x" * 600 in fenced
        assert "x" * 601 not in fenced

    async def test_live_cap_reads_setting(self, client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(session, {"a2a_fence_max_chars": 700})
        assert await live_fence_cap() == 700


class TestReaperWindowWiring:
    async def test_reap_honors_live_window(self, client: AsyncClient) -> None:
        from sqlalchemy import delete

        from app.ambient.execute import reap_stalled_runs
        from app.models import Conversation, Run

        async def make_stalled(age_s: int) -> None:
            async with get_session_factory()() as session:
                await session.execute(delete(Run))
                conv = Conversation(title="m40 reaper probe")
                session.add(conv)
                await session.flush()
                session.add(
                    Run(
                        conversation_id=conv.id,
                        chat_message="m40 reaper probe",
                        status="running",
                        trigger={"kind": "test"},
                        started_at=datetime.now(UTC) - timedelta(seconds=age_s),
                        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=age_s),
                    )
                )
                await session.commit()

        # a 90s-silent run: reaped under a 60s window …
        async with get_session_factory()() as session:
            await update_settings(session, {"run_stall_after_s": 60})
        await make_stalled(90)
        assert await reap_stalled_runs() == 1

        # … and left alone under a 3600s window
        async with get_session_factory()() as session:
            await update_settings(session, {"run_stall_after_s": 3600})
        await make_stalled(90)
        assert await reap_stalled_runs() == 0


class TestRecursionLimitWiring:
    async def test_setting_reaches_cache(self, client: AsyncClient) -> None:
        from app.registry_cache import get_cache

        resp = await client.patch(f"{API}/settings", json={"agentic_recursion_limit": 250})
        assert resp.status_code == 200
        assert int(await get_cache().setting("agentic_recursion_limit")) == 250
