"""M24 soaks (spec §17.6).

`python soak.py sim`   — simulated-clock 3-day soak, in-process against the
test database: a daily cron routine, an AIMD poller with a quiet→burst→quiet
profile, and 2/day digests. Checks cadence, backoff, and caps over 72 hours
of simulated time in a couple of minutes of wall clock.

`python soak.py live [minutes]` — compressed live soak against the running
stack over HTTP: a 60s-interval routine executes real runs on the configured
model; afterwards reports fires, run outcomes, abstain rate, deliveries, and
stall count (must be 0).
"""

import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test"
)

STEP_S = 300  # 5-minute sim steps
DAYS = 3


async def sim() -> dict[str, Any]:
    from sqlalchemy import select, text

    from app.ambient.decide import process_event
    from app.ambient.deliver import flush_deliveries
    from app.ambient.drain import drain_once, register_executor, register_processor
    from app.ambient.patterns import advance_patterns
    from app.ambient.triggers import evaluate_schedules, poll_due_intents, register_poll_source
    from app.db import get_engine, get_session_factory
    from app.models import AmbientEvent, Base, Delivery, Routine, StandingIntent
    from app.registry_cache import reset_cache
    from app.settings_store import update_settings

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    reset_cache()
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "ambient_enabled": True,
                "default_model": "fake:scripted",
                "ambient_digest_times": ["09:30", "17:30"],
                "ambient_quiet_hours": ["22:00", "07:00"],
            },
        )
        session.add(
            Routine(
                name="daily-brief",
                prompt="daily",
                triggers=[{"type": "cron", "cron": "0 9 * * *"}],
            )
        )
        intent = StandingIntent(
            text="watch the feed",
            condition_type="event",
            compiled={"poll": {"source": "sim_feed"}, "filters": []},
            base_interval_s=300,
            current_interval_s=300,
            max_interval_s=3600,
        )
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
        intent_id = intent.id

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    sim_now = start
    # feed profile: quiet day 1, a burst midday day 2, quiet day 3
    burst_lo = start + timedelta(days=1, hours=12)
    burst_hi = burst_lo + timedelta(hours=1)

    async def feed(
        watermark: str | None, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        if burst_lo <= sim_now < burst_hi:
            return ([{"item": f"burst-{sim_now.isoformat()}"}], sim_now.isoformat())
        return ([], watermark)

    register_poll_source("sim_feed", feed)

    async def processor(event: AmbientEvent) -> Any:
        await advance_patterns(event)
        return await process_event(event)

    register_processor(processor)
    register_executor(None)

    from app.ambient.deliver import add_delivery

    poll_checks = 0
    interval_track: list[int] = []
    seeded: set[str] = set()
    t0 = time.monotonic()
    steps = int(DAYS * 24 * 3600 / STEP_S)
    for _step in range(steps):
        # two digest items arrive each sim day (08:00, 16:00) so the 2/day
        # digest cadence is actually exercised end-to-end
        for hour in (8, 16):
            key = f"{sim_now.date()}:{hour}"
            if sim_now.hour == hour and key not in seeded:
                seeded.add(key)
                await add_delivery(
                    category="soak", tier=2, urgency=2, title=f"soak item {key}"
                )
        await evaluate_schedules(now=sim_now)
        before = None
        async with get_session_factory()() as session:
            row = await session.get(StandingIntent, intent_id)
            if row is not None:
                before = row.last_checked_at
        await poll_due_intents(now=sim_now)
        async with get_session_factory()() as session:
            row = await session.get(StandingIntent, intent_id)
            if row is not None:
                if row.last_checked_at != before:
                    poll_checks += 1
                interval_track.append(row.current_interval_s)
        await drain_once()
        await flush_deliveries(now=sim_now)
        sim_now += timedelta(seconds=STEP_S)

    register_processor(None)
    register_poll_source("sim_feed", None)

    async with get_session_factory()() as session:
        events = list((await session.execute(select(AmbientEvent))).scalars())
        digests = list(
            (
                await session.execute(select(Delivery).where(Delivery.channel == "digest"))
            ).scalars()
        )
    by_kind = Counter(e.kind for e in events)
    fires_by_day = Counter(
        e.received_at.date().isoformat() for e in events if e.verdict == "fired"
    )
    digest_batches = sorted({d.delivered_at.isoformat() for d in digests if d.delivered_at})
    out = {
        "mode": "sim",
        "sim_days": DAYS,
        "wall_s": round(time.monotonic() - t0, 1),
        "events_by_kind": dict(by_kind),
        "schedule_fires": by_kind.get("routine_schedule", 0),
        "poll_checks": poll_checks,
        "poll_interval_min_s": min(interval_track) if interval_track else None,
        "poll_interval_max_s": max(interval_track) if interval_track else None,
        "poll_interval_final_s": interval_track[-1] if interval_track else None,
        "fires_by_day": dict(fires_by_day),
        "digest_batches": len(digest_batches),
    }
    print(json.dumps(out, indent=2))
    return out


async def live(minutes: int) -> dict[str, Any]:
    import httpx

    base = "http://localhost:8000/api/v1"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base}/routines",
            json={
                "name": f"soak-{int(time.time())}",
                "prompt": (
                    "You are the soak-test heartbeat. There is nothing to check; "
                    "confirm liveness in one short sentence, or abstain if you have "
                    "nothing to add beyond the previous beat."
                ),
                "triggers": [{"type": "interval", "seconds": 60}],
                "autonomy": "propose",
            },
        )
        resp.raise_for_status()
        rid = resp.json()["id"]
        print(f"soak routine {rid} — running {minutes} min")
        await asyncio.sleep(minutes * 60)
        await client.patch(f"{base}/routines/{rid}", json={"status": "paused"})
        ledger = (await client.get(f"{base}/ambient/ledger?limit=200")).json()["items"]
        deliveries = (await client.get(f"{base}/deliveries?limit=200")).json()["items"]
        runs_resp = (await client.get(f"{base}/runs?limit=100")).json()
    runs = runs_resp if isinstance(runs_resp, list) else runs_resp.get("items", [])
    routine_events = [e for e in ledger if e.get("routine_id") == rid]
    soak_runs = [
        r
        for r in runs
        if isinstance(r, dict) and (r.get("trigger") or {}).get("routine_id") == rid
    ]
    soak_run_ids = {r["id"] for r in soak_runs}
    statuses = Counter(r.get("status") for r in soak_runs)
    abstained = sum(
        1
        for d in deliveries
        if d["category"] == "abstained" and d.get("run_id") in soak_run_ids
    )
    out = {
        "mode": "live",
        "minutes": minutes,
        "routine": rid,
        "events": len(routine_events),
        "verdicts": dict(Counter(e["verdict"] for e in routine_events)),
        "run_statuses": dict(statuses),
        "stalled": statuses.get("stalled", 0),
        "abstained_deliveries": abstained,
        "total_deliveries": len(deliveries),
    }
    print(json.dumps(out, indent=2))
    return out


async def main() -> None:
    from app.llm.registry import register_builtin_providers

    register_builtin_providers()
    mode = sys.argv[1] if len(sys.argv) > 1 else "sim"
    here = os.path.dirname(os.path.abspath(__file__))
    if mode == "sim":
        result = await sim()
    else:
        minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        result = await live(minutes)
    with open(os.path.join(here, f"result_soak_{mode}.json"), "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
