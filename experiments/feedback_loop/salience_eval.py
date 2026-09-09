"""FLE-2 harness (feedback_loop_exp): the salience judge's decision loop
replayed through the SHIPPED pipeline — real `add_delivery`, real flush,
real `run_salience_pass` in propose, real `decide()` writing
`judge_reward` — with a scripted judge and a scripted human as ground
truth. No LLM, no keys, deterministic.

World: three categories with different true value —
  prod-incidents  urgency 4-5, endorsement 0.9  (escalations worth it)
  build-noise     urgency 3-4, endorsement 0.05 (judge keeps proposing junk)
  batch-info      urgency 1-2 (mostly below the floor; the cheap tier)

The BASELINE this file produces is the frontier any FLE-3 learner must
meet or beat: a static floor sweep (2..5), learners off. Metrics per
condition: proposal precision (applied / decided), judge-call cost,
missed-critical (prod-incidents urgency ≥ 4 that never reached the judge),
per-category endorsement.

Usage: PYTHONPATH=backend python experiments/feedback_loop/salience_eval.py
       (writes result_baseline.json beside this file)
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test"
)
os.environ.setdefault("FAKE_LLM_ENABLED", "1")

ROUNDS = 30
NOON_HOUR = 12

# (category, urgency pattern per round i, endorse pattern per proposal i)
WORLD = [
    ("prod-incidents", lambda i: 4 + (i % 2), lambda i: i % 10 != 0),  # ≈0.9
    ("build-noise", lambda i: 3 + (i % 2), lambda i: i % 20 == 0),  # ≈0.05
    ("batch-info", lambda i: 1 + (i % 2), lambda i: i % 2 == 0),  # ≈0.5
]


async def _reset() -> None:
    from sqlalchemy import text

    from app.db import get_engine
    from app.models import Base
    from app.registry_cache import reset_cache

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    reset_cache()


async def run_condition(floor: int) -> dict[str, Any]:
    """One static condition: fixed prefilter floor, learners off."""
    from datetime import UTC, datetime

    import app.ambient.salience as salience
    from app.ambient.deliver import add_delivery, flush_deliveries
    from app.ambient.salience import SalienceVerdict, run_salience_pass
    from app.db import get_session_factory
    from app.models import Delivery
    from app.settings_store import update_settings
    from sqlalchemy import select

    await _reset()
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "ambient_enabled": True,
                "ambient_salience_mode": "propose",
                "ambient_salience_min_urgency": floor,
                "ambient_quiet_hours": ["03:00", "03:01"],
                # the M23 budget throttles tier-0 flushes to 3/day by
                # design; this harness studies the JUDGE loop, so the
                # delivery plane runs unthrottled
                "ambient_notification_budget_per_day": 100000,
                "ambient_escalation_budget_per_day": 100000,
                "ambient_digest_times": ["23:58"],
                "default_model": "fake:scripted",
            },
        )

    # scripted judge: an EAGER escalator — the human decisions are what
    # rate it, which is exactly the loop under test
    async def scripted_judge(_row: Delivery) -> SalienceVerdict:
        return SalienceVerdict(verdict="escalate", reason="scripted", confidence=0.9)

    real_judge = salience.judge
    salience.judge = scripted_judge  # in-process, restored below
    noon = datetime.now(UTC).replace(hour=NOON_HOUR, minute=0, second=0)
    judged_total = 0
    decided: dict[str, list[bool]] = {}
    try:
        for i in range(ROUNDS):
            for cat, urgency_fn, _ in WORLD:
                await add_delivery(
                    category=cat, tier=0, urgency=urgency_fn(i), title=f"{cat} {i}"
                )
            await flush_deliveries(noon)  # nobody watching ⇒ unseen
            out = await run_salience_pass(limit=50)
            judged_total += out["judged"]

            # the scripted human decides every fresh proposal via the
            # SHIPPED decision engine — judge_reward lands the real way
            async with get_session_factory()() as session:
                fresh = list(
                    (
                        await session.execute(
                            select(Delivery).where(Delivery.salience.isnot(None))
                        )
                    ).scalars()
                )
            for row in fresh:
                if row.salience.get("decision"):
                    continue
                n = len(decided.setdefault(row.category, []))
                endorse = dict((c, e) for c, _, e in WORLD)[row.category](n)
                await salience.decide(row.id, "apply" if endorse else "decline")
                decided[row.category].append(endorse)
    finally:
        salience.judge = real_judge

    async with get_session_factory()() as session:
        rows = list((await session.execute(select(Delivery))).scalars())
    proposals = [r for r in rows if r.salience]
    applied = sum(1 for r in proposals if r.salience.get("decision") == "applied")
    declined = sum(1 for r in proposals if r.salience.get("decision") == "declined")
    precision = applied / (applied + declined) if applied + declined else None
    missed_critical = sum(
        1
        for r in rows
        if r.category == "prod-incidents" and r.urgency >= 4 and not r.salience
    )
    return {
        "floor": floor,
        "judge_calls": judged_total,
        "proposals_decided": applied + declined,
        "proposal_precision": round(precision, 3) if precision is not None else None,
        "missed_critical": missed_critical,
        "per_category_endorsement": {
            c: round(sum(v) / len(v), 3) for c, v in decided.items() if v
        },
        "total_judge_reward": round(
            sum(r.salience.get("judge_reward") or 0 for r in proposals), 1
        ),
    }


async def run_learner_condition(start_floor: int) -> dict[str, Any]:
    """The FLE-3 tuner in auto, invoked every 5 rounds (the M25 cadence),
    against the identical stream. The bar: beat the best static point
    (floor 4: precision .622) at zero missed-critical, zero clamp
    violations."""
    from datetime import UTC, datetime

    import app.ambient.salience as salience
    from app.ambient.deliver import add_delivery, flush_deliveries
    from app.ambient.salience import SalienceVerdict, run_salience_pass
    from app.ambient.salience_learn import run_salience_tuner
    from app.db import get_session_factory
    from app.models import AmbientPolicy, Delivery
    from app.registry_cache import get_cache
    from app.settings_store import update_settings
    from sqlalchemy import select

    await _reset()
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "ambient_enabled": True,
                "ambient_salience_mode": "propose",
                "ambient_salience_min_urgency": start_floor,
                "ambient_salience_learning": "auto",
                "ambient_quiet_hours": ["03:00", "03:01"],
                "ambient_notification_budget_per_day": 100000,
                "ambient_escalation_budget_per_day": 100000,
                "ambient_digest_times": ["23:58"],
                "default_model": "fake:scripted",
            },
        )

    async def scripted_judge(_row: Delivery) -> SalienceVerdict:
        return SalienceVerdict(verdict="escalate", reason="scripted", confidence=0.9)

    real_judge = salience.judge
    salience.judge = scripted_judge
    noon = datetime.now(UTC).replace(hour=NOON_HOUR, minute=0, second=0)
    judged_total = 0
    decided: dict[str, list[bool]] = {}
    floor_track = [start_floor]
    clamp_violations = 0
    try:
        for i in range(ROUNDS):
            for cat, urgency_fn, _ in WORLD:
                await add_delivery(
                    category=cat, tier=0, urgency=urgency_fn(i), title=f"{cat} {i}"
                )
            await flush_deliveries(noon)
            out = await run_salience_pass(limit=50)
            judged_total += out["judged"]
            async with get_session_factory()() as session:
                fresh = list(
                    (
                        await session.execute(
                            select(Delivery).where(Delivery.salience.isnot(None))
                        )
                    ).scalars()
                )
            for row in fresh:
                if row.salience.get("decision"):
                    continue
                n = len(decided.setdefault(row.category, []))
                endorse = dict((c, e) for c, _, e in WORLD)[row.category](n)
                await salience.decide(row.id, "apply" if endorse else "decline")
                decided[row.category].append(endorse)
            if (i + 1) % 5 == 0:
                await run_salience_tuner(force=True)
                floor = int(await get_cache().setting("ambient_salience_min_urgency"))
                floor_track.append(floor)
                if not 2 <= floor <= 5:
                    clamp_violations += 1
    finally:
        salience.judge = real_judge

    async with get_session_factory()() as session:
        rows = list((await session.execute(select(Delivery))).scalars())
        policies = list((await session.execute(select(AmbientPolicy))).scalars())
    proposals = [r for r in rows if r.salience]
    applied = sum(1 for r in proposals if r.salience.get("decision") == "applied")
    declined = sum(1 for r in proposals if r.salience.get("decision") == "declined")
    precision = applied / (applied + declined) if applied + declined else None
    return {
        "mode": "learner_auto",
        "start_floor": start_floor,
        "judge_calls": judged_total,
        "proposals_decided": applied + declined,
        "proposal_precision": round(precision, 3) if precision is not None else None,
        "missed_critical": sum(
            1
            for r in rows
            if r.category == "prod-incidents" and r.urgency >= 4 and not r.salience
        ),
        "per_category_endorsement": {
            c: round(sum(v) / len(v), 3) for c, v in decided.items() if v
        },
        "floor_track": floor_track,
        "clamp_violations": clamp_violations,
        "mutes": sorted(
            p.category for p in policies if p.category.startswith("salience:")
        ),
        "total_judge_reward": round(
            sum(r.salience.get("judge_reward") or 0 for r in proposals), 1
        ),
    }


async def main() -> None:
    from app.llm.registry import register_builtin_providers

    register_builtin_providers()
    out = {
        "baseline_floor_sweep": [await run_condition(f) for f in (2, 3, 4, 5)],
        "learner": await run_learner_condition(3),
    }
    path = Path(__file__).with_name("result_baseline.json")
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
