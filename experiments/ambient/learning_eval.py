"""M25 gate (spec milestone table): learning-on ≥ static policy on
intervention precision, with ZERO learner-caused tier-0 escalations.

Deterministic simulation over the real substrate (no LLM): two categories —
'ops-alerts' starts at tier 1 (notify) but is 80% dismissed; 'digest-reports'
starts at tier 2 (digest) and is 80% accepted. 30 feedback rounds each.

  static    ambient_learning_mode=off  → only the M23 precision rule acts
  learning  ambient_learning_mode=auto → the §17.7 learner acts (both
            directions), forced every 5 rounds

Intervention precision = accepted / (accepted + dismissed) over items whose
EFFECTIVE tier at insert was ≤ 1 — "when we interrupted or notified, was it
worth it". Also reports total blended reward and tier-0 escalations.

Usage: python learning_eval.py   (writes result_learning_eval.json)
"""

import asyncio
import json
import os
from typing import Any

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test"
)

ROUNDS = 30


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


async def run_condition(mode: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app.ambient.deliver import add_delivery, record_feedback
    from app.ambient.learn import run_learner
    from app.db import get_session_factory
    from app.models import AmbientPolicy, Delivery
    from app.settings_store import update_settings

    await _reset()
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "ambient_enabled": True,
                "ambient_learning_mode": mode,
                "default_model": "fake:scripted",
            },
        )

    # deterministic 80/20 feedback pattern: index%5==0 flips the majority
    for i in range(ROUNDS):
        a = await add_delivery(
            category="ops-alerts", tier=1, urgency=4, title=f"alert {i}"
        )
        await record_feedback(a.id, "accepted" if i % 5 == 0 else "dismissed")
        b = await add_delivery(
            category="digest-reports", tier=2, urgency=3, title=f"report {i}"
        )
        await record_feedback(b.id, "dismissed" if i % 5 == 0 else "accepted")
        if mode == "auto" and (i + 1) % 5 == 0:
            await run_learner(force=True)

    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(Delivery).where(Delivery.feedback.isnot(None))
                )
            ).scalars()
        )
        policies = list((await session.execute(select(AmbientPolicy))).scalars())
    intrusive = [d for d in rows if d.tier <= 1]
    accepted = sum(1 for d in intrusive if d.feedback == "accepted")
    dismissed = sum(1 for d in intrusive if d.feedback == "dismissed")
    precision = accepted / (accepted + dismissed) if accepted + dismissed else None
    tier_track: dict[str, list[int]] = {}
    for d in sorted(rows, key=lambda r: r.created_at):
        tier_track.setdefault(d.category, []).append(d.tier)
    return {
        "mode": mode,
        "interventions": len(intrusive),
        "intervention_precision": round(precision, 3) if precision is not None else None,
        "total_reward": round(sum(d.reward or 0 for d in rows), 2),
        "tier0_escalations": sum(
            1 for p in policies if p.tier_override == 0
        ),  # must be 0 by construction
        "final_tiers": {c: t[-1] for c, t in tier_track.items()},
        "policy_rows": [
            {"category": p.category, "override": p.tier_override, "source": p.source}
            for p in policies
            if p.category != "digest_anchor"
        ],
    }


async def main() -> None:
    from app.llm.registry import register_builtin_providers

    register_builtin_providers()
    out = {
        "static": await run_condition("off"),
        "learning": await run_condition("auto"),
    }
    s, learn = out["static"], out["learning"]
    out["gate"] = {
        "learning_ge_static_precision": bool(
            (learn["intervention_precision"] or 0) >= (s["intervention_precision"] or 0)
        ),
        "zero_learner_tier0_escalations": learn["tier0_escalations"] == 0,
    }
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "result_learning_eval.json")
    with open(path, "w") as f:  # noqa: ASYNC230 - experiment script
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    print(f"→ {path}")


if __name__ == "__main__":
    asyncio.run(main())
