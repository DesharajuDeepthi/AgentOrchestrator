"""Long-horizon time-warp simulation (spec §16.7 / milestone M18).

Deterministic, no live stack and no LLM required: seeds a ~90-day backdated
store into a scratch Postgres (the test DB by default), then drives the
consolidation jobs directly — decay sweep, contradiction sweep, digest
compaction — and checks the equilibrium the design promises:

  E1  untouched low-importance memories expire
  E2  rehearsed (recently accessed) memories survive
  E3  pinned memories are immune regardless of age
  E4  high-importance memories outlive low-importance peers of the same age
  E5  the episodic store compacts to O(conversations), not O(runs)
  E6  duplicate active entity_keys are quarantined, oldest-validity wins

Usage: python longhorizon.py   (writes result_longhorizon.json)
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test",
)
os.environ["FAKE_LLM_ENABLED"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

DAYS = 90


async def seed_and_run() -> dict[str, Any]:
    from sqlalchemy import select, text, update

    from app.config import get_config
    from app.db import get_engine, get_session_factory, reset_db_state
    from app.llm.registry import register_builtin_providers
    from app.memory.episodic import compact_digests
    from app.memory.lifecycle import contradiction_sweep, decay_sweep
    from app.memory.store import remember
    from app.models import Base, Conversation, Memory, Run, RunDigest
    from app.settings_store import update_settings

    get_config.cache_clear()
    register_builtin_providers()
    reset_db_state()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    sf = get_session_factory()
    async with sf() as session:
        await update_settings(
            session,
            {
                "memory_enabled": True,
                "embedding_model": "fake:scripted",
                "memory_half_life_days": 30.0,
                "memory_digest_compact_days": 14,
            },
        )

    now = datetime.now(UTC)

    async def backdate(memory_id: Any, days: float, accessed_days_ago: float | None = None) -> None:
        async with sf() as session:
            await session.execute(
                update(Memory)
                .where(Memory.id == memory_id)
                .values(
                    recorded_at=now - timedelta(days=days),
                    valid_from=now - timedelta(days=days),
                    last_accessed_at=now
                    - timedelta(days=accessed_days_ago if accessed_days_ago is not None else days),
                )
            )
            await session.commit()

    # ── semantic store: four cohorts across 90 days ──────────────────
    cohorts = {"stale_low": [], "rehearsed_low": [], "stale_high": [], "pinned_old": []}
    for i in range(10):  # importance 3, never touched since day-90..day-60
        m = await remember(
            text=f"trivial detail number {i} from the earliest era of the project",
            kind="fact",
            source="user_stated",
            importance=3,
        )
        await backdate(m.id, DAYS - i * 3)
        cohorts["stale_low"].append(str(m.id))
    for i in range(10):  # importance 3 but rehearsed within the last week
        m = await remember(
            text=f"rehearsed detail number {i} the user keeps coming back to",
            kind="fact",
            source="user_stated",
            importance=3,
        )
        await backdate(m.id, DAYS - i * 3, accessed_days_ago=float(i % 7))
        cohorts["rehearsed_low"].append(str(m.id))
    for i in range(10):  # importance 9, untouched — identity-level facts
        m = await remember(
            text=f"identity-level fact number {i} about the user's world",
            kind="fact",
            source="user_stated",
            importance=9,
        )
        await backdate(m.id, DAYS - i * 3)
        cohorts["stale_high"].append(str(m.id))
    for i in range(3):  # pinned, ancient, low importance
        m = await remember(
            text=f"pinned profile row number {i} — always relevant",
            kind="preference",
            source="user_stated",
            importance=2,
        )
        async with sf() as session:
            await session.execute(update(Memory).where(Memory.id == m.id).values(pinned=True))
            await session.commit()
        await backdate(m.id, DAYS)
        cohorts["pinned_old"].append(str(m.id))

    # contradiction seed: two ACTIVE rows sharing an entity_key
    # importance 8 so decay spares both — the sweep, not decay, must resolve
    a = await remember(
        text="the deploy branch is main",
        kind="fact",
        source="user_stated",
        entity_key="deploy",
        importance=8,
    )
    b = await remember(
        text="the deploy branch is release-2026",
        kind="fact",
        source="user_stated",
        entity_key="deploy",
        importance=8,
    )
    await backdate(a.id, 80)
    await backdate(b.id, 10)

    # ── episodic store: 8 conversations × ~11 runs over 90 days ─────
    async with sf() as session:
        for c in range(8):
            conv = Conversation(id=uuid4(), title=f"sim conversation {c}")
            session.add(conv)
            await session.flush()
            for r in range(11):
                age = DAYS - (c * 11 + r)
                run = Run(
                    id=uuid4(),
                    conversation_id=conv.id,
                    chat_message=f"simulated ask {r} in conversation {c}",
                    status="completed",
                    orchestrator_mode="graph",
                )
                session.add(run)
                await session.flush()
                session.add(
                    RunDigest(
                        run_id=run.id,
                        conversation_id=conv.id,
                        kind="run",
                        text=f"Asked: simulated ask {r} in conversation {c} — outcome: completed.",
                        created_at=now - timedelta(days=max(age, 0)),
                    )
                )
        await session.commit()

    async with sf() as session:
        digests_before = len(list((await session.execute(select(RunDigest))).scalars()))

    # ── drive the consolidation jobs (the "90 days pass" moment) ─────
    expired = await decay_sweep()
    quarantined = await contradiction_sweep()
    folded = await compact_digests()

    async def status_of(ids: list[str]) -> dict[str, int]:
        async with sf() as session:
            rows = list(
                (
                    await session.execute(select(Memory).where(Memory.id.in_(ids)))
                ).scalars()
            )
        out: dict[str, int] = {}
        for r in rows:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    async with sf() as session:
        digests_after = list((await session.execute(select(RunDigest))).scalars())
        periods = [d for d in digests_after if d.kind == "period"]
        raw_left = [d for d in digests_after if d.kind == "run"]
        deploy_rows = list(
            (
                await session.execute(select(Memory).where(Memory.entity_key == "deploy"))
            ).scalars()
        )

    checks = {
        "E1_stale_low_expired": (await status_of(cohorts["stale_low"])).get("expired", 0) >= 8,
        "E2_rehearsed_low_survive": (await status_of(cohorts["rehearsed_low"])).get("active", 0)
        == 10,
        "E3_pinned_immune": (await status_of(cohorts["pinned_old"])).get("active", 0) == 3,
        "E4_stale_high_survive": (await status_of(cohorts["stale_high"])).get("active", 0) == 10,
        # conversations 0-6 hold digests older than the 14-day cutoff;
        # conversation 7 is entirely recent and must stay raw
        "E5_episodic_bounded": len(periods) == 7
        and len(raw_left) == 11
        and all((now - d.created_at).days < 14 for d in raw_left),
        "E6_contradiction_quarantined": quarantined == 1
        and sorted(m.status for m in deploy_rows) == ["active", "quarantined"]
        and next(m for m in deploy_rows if m.status == "active").text.endswith("main"),
    }
    report = {
        "days_simulated": DAYS,
        "expired": expired,
        "quarantined": quarantined,
        "digests_folded": folded,
        "digests_before": digests_before,
        "period_digests": len(periods),
        "raw_digests_left": len(raw_left),
        "cohorts": {k: await status_of(v) for k, v in cohorts.items()},
        "checks": checks,
        "all_pass": all(checks.values()),
    }
    await engine.dispose()
    return report


if __name__ == "__main__":
    report = asyncio.run(seed_and_run())
    print(json.dumps(report, indent=2))
    with open(os.path.join(os.path.dirname(__file__), "result_longhorizon.json"), "w") as f:
        json.dump(report, f, indent=2)
    sys.exit(0 if report["all_pass"] else 1)
