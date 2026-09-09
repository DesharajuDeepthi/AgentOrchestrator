"""Salience tuner (feedback_loop_exp — FLE-3): the first consumer of the
M43b `judge_reward` ledger, entering under the §17.7 feedback-consumer
rule — its own gate (`ambient_salience_learning`, off|propose|auto,
default off, born dark), hard clamps, deterministic rules (no bandit:
salience decisions are rare events; see docs/research/feedback_loop/).

Two adjustments, both riding the EXISTING policy ledger so revert
(§17.7), reject (M44) and the proposal review UI work with no new
machinery:

- **per-category mutes** — `salience:<cat>` rows; tier_override=1 means
  "stop judging this category", a clearing row (None) un-mutes, which is
  exactly what the revert endpoint appends.
- **urgency-floor moves** — `setting:ambient_salience_min_urgency` rows
  through the same `_apply_special` path the digest-time learner uses;
  ±1 per invocation, clamped to [2, 5].

Rule change vs the research doc, forced by the harness: the doc said a
mute needs "zero endorsements"; a noise category that is still 5%%
valuable never hits zero in a rolling window, so the trigger is
endorsement ≤ MUTE_MAX_ENDORSEMENT over ≥ MUTE_MIN decided. Recorded
here and in the experiment report rather than silently adjusted.
"""

from typing import Any

import structlog
from sqlalchemy import select

from app.db import get_session_factory
from app.models import Delivery

logger = structlog.get_logger("ambient")

WINDOW = 20  # per-category sliding window, mirroring §17.3's shape
MUTE_MIN = 5  # decided verdicts before a mute may even be considered
MUTE_MAX_ENDORSEMENT = 0.10
FLOOR_MIN, FLOOR_MAX = 2, 5
FLOOR_MIN_SAMPLE = 10
FLOOR_UP_BELOW = 0.30  # overall endorsement under this ⇒ floor + 1
FLOOR_DOWN_ABOVE = 0.80  # over this ⇒ floor − 1 (the judge is starving)


async def salience_muted(category: str, user_id: Any = None) -> bool:
    """Is judging muted for this category? Latest applied `salience:<cat>`
    ledger row wins; a clearing row (revert) un-mutes."""
    from app.ambient.deliver import current_tier_override

    return await current_tier_override(f"salience:{category}", user_id) == 1


async def run_salience_tuner(force: bool = False) -> dict[str, int]:
    """One tuner pass over the decided `judge_reward` ledger."""
    from app.ambient.learn import apply_policy
    from app.registry_cache import get_cache
    from app.settings_store import update_settings

    out = {"considered": 0, "mutes": 0, "floor_moves": 0}
    mode = str(await get_cache().setting("ambient_salience_learning") or "off")
    if mode == "off":
        return out
    source = "learner" if mode == "auto" else "learner_proposal"

    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(Delivery)
                    .where(Delivery.salience.isnot(None))
                    .order_by(Delivery.created_at.desc())
                )
            ).scalars()
        )
    # per-category window over DECIDED verdicts; undone counts as
    # repudiation (the human reversed what the judge caused)
    per_cat: dict[str, list[bool]] = {}
    for row in rows:
        decision = (row.salience or {}).get("decision")
        if decision not in ("applied", "declined", "undone"):
            continue
        bucket = per_cat.setdefault(row.category, [])
        if len(bucket) < WINDOW:
            bucket.append(decision == "applied")
    out["considered"] = sum(len(v) for v in per_cat.values())

    # 1) per-category mutes
    for cat, decisions in per_cat.items():
        if len(decisions) < MUTE_MIN or await salience_muted(cat):
            continue
        rate = sum(decisions) / len(decisions)
        if rate <= MUTE_MAX_ENDORSEMENT:
            await apply_policy(
                category=f"salience:{cat}",
                tier_override=1,
                reason=(
                    f"salience learner: {len(decisions) - sum(decisions)}/"
                    f"{len(decisions)} verdicts repudiated (endorsement {rate:.2f})"
                ),
                source=source,
            )
            out["mutes"] += 1
            logger.info(
                "salience_learner_mute",
                tier="ambient",
                kind="deliver",
                category=cat,
                endorsement=round(rate, 3),
                mode=mode,
            )

    # 2) one urgency-floor move per invocation, judged over the un-muted mix
    live: list[bool] = []
    for cat, decisions in per_cat.items():
        if not await salience_muted(cat):
            live.extend(decisions)
    if len(live) >= FLOOR_MIN_SAMPLE:
        rate = sum(live) / len(live)
        floor = int(await get_cache().setting("ambient_salience_min_urgency") or 3)
        target = None
        if rate < FLOOR_UP_BELOW and floor < FLOOR_MAX:
            target = floor + 1
        elif rate > FLOOR_DOWN_ABOVE and floor > FLOOR_MIN:
            target = floor - 1
        if target is not None:
            reason = (
                f"salience learner: endorsement {rate:.2f} over {len(live)} decided "
                f"|| proposed={target}"
            )
            if mode == "auto":
                async with get_session_factory()() as session:
                    await update_settings(session, {"ambient_salience_min_urgency": target})
            await apply_policy(
                category="setting:ambient_salience_min_urgency",
                tier_override=None,
                reason=reason,
                source=source,
            )
            out["floor_moves"] += 1
            logger.info(
                "salience_learner_floor",
                tier="ambient",
                kind="deliver",
                floor=floor,
                proposed=target,
                endorsement=round(rate, 3),
                mode=mode,
            )
    return out
