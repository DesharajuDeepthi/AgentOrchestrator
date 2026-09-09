"""Adaptive policy learning (spec §17.7 — milestone M25).

A bandit-style learner over the M23 reward substrate. Three adjustment
families: category re-tiering (mean reward decides direction, one step at a
time, NEVER into tier 0), digest-time shifting (toward observed acceptance
hours, clamped to ±2h of the ledgered anchor — the user-configured times at
the moment learning first touched them), and per-intent judge thresholds
(`budget.min_urgency`, raised when a watch's fires are chronically
dismissed).

Governance: `ambient_learning_mode` — `off` collects only (byte-identical:
the learner never runs), `auto` is the PRIMARY mode (changes apply
immediately within the clamps, every change an append-only ledger row,
one-click revertible), `propose` queues each change as a
`learner_proposal` row + a review item in the inbox; it applies only on
approval. Runs as a consolidation-class job on the ambient tick, at most
once per interval per process."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select

from app.db import get_session_factory
from app.models import AmbientPolicy, Delivery, StandingIntent

logger = structlog.get_logger("ambient")

LEARN_MIN_SAMPLE = 3  # §14c-28: three dismissals is a signal
LEARN_WINDOW = 20
DEMOTE_BELOW = -0.3  # mean reward thresholds (blend, not raw acceptance)
PROMOTE_ABOVE = 0.6
PROMOTE_MIN_SAMPLE = 5
DIGEST_SHIFT_CLAMP_H = 2
MIN_URGENCY_CAP = 5
_INTERVAL_S = 3600
_last_run: datetime | None = None


async def apply_policy(
    *,
    category: str,
    tier_override: int | None,
    reason: str,
    source: str,
    user_id: "UUID | None" = None,
) -> AmbientPolicy:
    """The single application path for category overrides. Hard clamp:
    an override may NEVER place a category into tier 0 (spec §17.7)."""
    if tier_override is not None and tier_override < 1:
        raise ValueError("policy overrides may never move a category into tier 0")
    async with get_session_factory()() as session:
        row = AmbientPolicy(
            user_id=user_id,
            category=category,
            tier_override=tier_override,
            reason=reason,
            source=source,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    from app import obs

    obs.AMBIENT_OPS.labels(kind="deliver", status=f"policy_{source}").inc()
    logger.info(
        "ambient_policy",
        tier="ambient",
        kind="deliver",
        category=category,
        override=tier_override,
        source=source,
    )
    return row


async def _queue_proposal(
    category: str, tier_override: int | None, reason: str, user_id: "UUID | None" = None
) -> None:
    """Propose mode: the change is ledgered as a proposal (inert until
    approval) and a review item rides the inbox digest (§16.2 pattern)."""
    from app.ambient.deliver import add_delivery

    row = await apply_policy(
        category=category,
        tier_override=tier_override,
        reason=reason,
        source="learner_proposal",
    )
    await add_delivery(
        category="learning",
        tier=2,
        urgency=3,
        title=f"Learning proposal: {reason[:180]}",
        body=(
            f"Proposed change for '{category}': tier_override={tier_override}. "
            f"Approve via POST /api/v1/ambient/policies/{row.id}/approve — "
            "nothing applies until you do."
        ),
        skey=f"learning:{category}",
    )


async def approve_proposal(policy_id: UUID) -> AmbientPolicy | None:
    """Turn a queued learner_proposal into an applied learner row."""
    async with get_session_factory()() as session:
        row = await session.get(AmbientPolicy, policy_id)
    if row is None or row.source != "learner_proposal":
        return None
    if row.category.startswith("setting:") or row.category.startswith("intent:"):
        await _apply_special(row.category, row.reason)
        return await apply_policy(
            category=row.category,
            tier_override=None,
            reason=f"approved: {row.reason}",
            source="learner",
        )
    return await apply_policy(
        category=row.category,
        tier_override=row.tier_override,
        reason=f"approved: {row.reason}",
        source="learner",
    )


async def reject_proposal(policy_id: UUID) -> AmbientPolicy | None:
    """M44 §17.7: explicitly reject a queued proposal — capture-only. The
    row is re-marked source='learner_rejected' (still inert everywhere the
    ledger computes effective policy) instead of sitting pending forever;
    a rejected proposal is itself feedback about the learner."""
    from datetime import UTC, datetime

    async with get_session_factory()() as session:
        row = await session.get(AmbientPolicy, policy_id)
        if row is None or row.source != "learner_proposal":
            return None
        row.source = "learner_rejected"
        row.reason = f"rejected {datetime.now(UTC).isoformat()}: {row.reason}"[:2000]
        await session.commit()
        await session.refresh(row)
    from app import obs

    obs.AMBIENT_OPS.labels(kind="deliver", status="policy_rejected").inc()
    logger.info(
        "ambient_proposal_rejected",
        tier="ambient",
        kind="deliver",
        policy_id=str(policy_id),
        category=row.category,
    )
    return row


def _mean_reward(rows: list[Delivery]) -> float | None:
    rewards = [d.reward for d in rows if d.reward is not None]
    if not rewards:
        return None
    return sum(rewards) / len(rewards)


async def _category_signals(
    owner: "UUID | None" = None, scoped: bool = False
) -> list[tuple[str, int, float, int]]:
    """(category, current_effective_tier, mean_reward, judged_n) for every
    category with judged feedback — the learner's observation."""
    from app.ambient.deliver import current_tier_override

    async with get_session_factory()() as session:
        cat_query = select(Delivery.category).where(Delivery.feedback.isnot(None)).distinct()
        if scoped:
            cat_query = cat_query.where(
                Delivery.user_id == owner if owner is not None else Delivery.user_id.is_(None)
            )
        categories = list((await session.execute(cat_query)).scalars())
        out: list[tuple[str, int, float, int]] = []
        for category in categories:
            if category == "learning":
                continue  # never learn on our own review items
            row_query = (
                select(Delivery)
                .where(Delivery.category == category, Delivery.feedback.isnot(None))
                .order_by(Delivery.created_at.desc())
                .limit(LEARN_WINDOW)
            )
            if scoped:
                row_query = row_query.where(
                    Delivery.user_id == owner if owner is not None else Delivery.user_id.is_(None)
                )
            rows = list((await session.execute(row_query)).scalars())
            mean = _mean_reward(rows)
            if mean is None:
                continue
            override = await current_tier_override(category, owner if scoped else None)
            tier = override if override is not None else int(rows[0].tier)
            out.append((category, tier, mean, len(rows)))
    return out


async def _learn_retiers(mode: str) -> int:
    """§18.8: with auth on the learner observes and re-tiers each owner's
    delivery pool separately (policies carry user_id)."""
    from app.auth import auth_enabled

    scoped = auth_enabled()
    owners: list[UUID | None] = [None]
    if scoped:
        async with get_session_factory()() as session:
            owners = sorted(
                {
                    row[0]
                    for row in (
                        await session.execute(
                            select(Delivery.user_id).where(Delivery.feedback.isnot(None)).distinct()
                        )
                    ).all()
                },
                key=str,
            )
    changed = 0
    for owner in owners:
        changed += await _learn_retiers_for(mode, owner, scoped)
    return changed


async def _learn_retiers_for(mode: str, owner: "UUID | None", scoped: bool) -> int:
    changed = 0
    for category, tier, mean, n in await _category_signals(owner, scoped):
        target: int | None = None
        if n >= LEARN_MIN_SAMPLE and mean <= DEMOTE_BELOW and tier < 3:
            target = tier + 1
        elif n >= PROMOTE_MIN_SAMPLE and mean >= PROMOTE_ABOVE and tier > 1:
            target = tier - 1  # clamp: tier 1 floor — never into interrupt
        if target is None:
            continue
        from app.ambient.deliver import current_tier_override

        if await current_tier_override(category, owner if scoped else None) == target:
            continue
        direction = "demoted" if target > tier else "promoted"
        reason = f"mean reward {mean:+.2f} over {n} judged items — {direction} tier {tier}→{target}"
        if mode == "auto":
            await apply_policy(
                category=category,
                tier_override=target,
                reason=reason,
                source="learner",
                user_id=owner if scoped else None,
            )
        else:
            await _queue_proposal(category, target, reason, owner if scoped else None)
        changed += 1
    return changed


def _parse_hhmm(value: str) -> int:
    hh, mm = (int(x) for x in value.split(":"))
    return hh * 60 + mm


def _fmt_hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def _digest_anchor(current: list[str]) -> list[str]:
    """The clamp anchor: the configured times when learning first touched
    them — ledgered so drift is bounded forever, not per step."""
    async with get_session_factory()() as session:
        row = (
            (
                await session.execute(
                    select(AmbientPolicy)
                    .where(AmbientPolicy.category == "digest_anchor")
                    .order_by(AmbientPolicy.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    if row is not None:
        return row.reason.split(",")
    await apply_policy(
        category="digest_anchor",
        tier_override=None,
        reason=",".join(current),
        source="learner",
    )
    return list(current)


async def _learn_digest_times(mode: str) -> int:
    from app.auth import auth_enabled

    if auth_enabled():
        # §18.8: digest times live per user (users.prefs) in the multi-user
        # regime — a GLOBAL shift learned from one user's feedback would
        # leak preference across tenants, so the global learner stands down
        logger.info("ambient_learn_digest_skipped_multiuser", tier="ambient", kind="learn")
        return 0
    return await _learn_digest_times_global(mode)


async def _learn_digest_times_global(mode: str) -> int:
    """Shift each digest time toward the mean hour of recently ACCEPTED
    digest deliveries, clamped to ±2h of the anchor."""
    from app.registry_cache import get_cache
    from app.settings_store import update_settings

    cache = get_cache()
    current = [str(x) for x in (await cache.setting("ambient_digest_times") or [])]
    if not current:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=14)
    async with get_session_factory()() as session:
        accepted = list(
            (
                await session.execute(
                    select(Delivery).where(
                        Delivery.channel == "digest",
                        Delivery.feedback == "accepted",
                        Delivery.delivered_at >= cutoff,
                    )
                )
            ).scalars()
        )
    if len(accepted) < LEARN_MIN_SAMPLE:
        return 0
    anchor = await _digest_anchor(current)
    if len(anchor) != len(current):
        anchor = list(current)
    # §18.1 multi-time shifting: each accepted delivery votes for its
    # NEAREST configured time; every slot with enough votes shifts toward
    # its own cluster mean, each under its own ±2h anchor clamp
    votes: dict[int, list[int]] = {}
    for d in accepted:
        if d.delivered_at is None:
            continue
        minute = d.delivered_at.hour * 60 + d.delivered_at.minute
        idx = min(range(len(current)), key=lambda i: abs(_parse_hhmm(current[i]) - minute))
        votes.setdefault(idx, []).append(minute)
    proposed = list(current)
    shifts: list[str] = []
    for idx, minutes in votes.items():
        if len(minutes) < LEARN_MIN_SAMPLE:
            continue
        mean_minute = int(sum(minutes) / len(minutes))
        lo = _parse_hhmm(anchor[idx]) - DIGEST_SHIFT_CLAMP_H * 60
        hi = _parse_hhmm(anchor[idx]) + DIGEST_SHIFT_CLAMP_H * 60
        target = max(lo, min(hi, mean_minute))
        if _fmt_hhmm(target) != current[idx]:
            proposed[idx] = _fmt_hhmm(target)
            shifts.append(f"{current[idx]}→{proposed[idx]}")
    if not shifts:
        return 0
    reason = (
        f"accepted digests cluster per-slot — shifting {', '.join(shifts)} "
        f"(anchors {','.join(anchor)}, clamp ±2h each)"
    )
    if mode == "auto":
        async with get_session_factory()() as session:
            await update_settings(session, {"ambient_digest_times": proposed})
        await apply_policy(
            category="setting:ambient_digest_times",
            tier_override=None,
            reason=reason,
            source="learner",
        )
    else:
        row = await apply_policy(
            category="setting:ambient_digest_times",
            tier_override=None,
            reason=f"{reason} || proposed={','.join(proposed)}",
            source="learner_proposal",
        )
        from app.ambient.deliver import add_delivery

        await add_delivery(
            category="learning",
            tier=2,
            urgency=3,
            title=f"Learning proposal: {reason[:180]}",
            body=f"Approve via POST /api/v1/ambient/policies/{row.id}/approve",
            skey="learning:digest_times",
        )
    return 1


async def _apply_special(category: str, reason: str) -> None:
    """Apply an approved non-tier proposal (settings / intent thresholds)."""
    from app.settings_store import update_settings

    if category == "setting:ambient_salience_min_urgency" and "proposed=" in reason:
        value = int(reason.rsplit("proposed=", 1)[1].split()[0])
        async with get_session_factory()() as session:
            await update_settings(session, {"ambient_salience_min_urgency": value})
    elif category == "setting:memory_admission_min_confidence" and "proposed=" in reason:
        floor = float(reason.rsplit("proposed=", 1)[1].split()[0])
        async with get_session_factory()() as session:
            await update_settings(session, {"memory_admission_min_confidence": floor})
    elif category == "setting:memory_quarantine_kinds" and "proposed=+" in reason:
        kind = reason.rsplit("proposed=+", 1)[1].split()[0]
        from app.registry_cache import get_cache

        routed = set(await get_cache().setting("memory_quarantine_kinds") or [])
        async with get_session_factory()() as session:
            await update_settings(session, {"memory_quarantine_kinds": sorted(routed | {kind})})
    elif category == "setting:ambient_digest_times" and "proposed=" in reason:
        proposed = reason.rsplit("proposed=", 1)[1].split(",")
        async with get_session_factory()() as session:
            await update_settings(session, {"ambient_digest_times": proposed})
    elif category.startswith("intent:") and "min_urgency=" in reason:
        intent_id = UUID(category.split(":")[1])
        value = int(reason.rsplit("min_urgency=", 1)[1].split()[0])
        async with get_session_factory()() as session:
            intent = await session.get(StandingIntent, intent_id)
            if intent is not None:
                intent.budget = {**(intent.budget or {}), "min_urgency": value}
                await session.commit()


async def _learn_intent_thresholds(mode: str) -> int:
    """A watch whose fires are chronically dismissed gets a higher judge
    bar: budget.min_urgency +1 (cap 5); decide holds fires below it. A
    watch earning its keep again RECOVERS: −1 per pass, never below the
    default of 2 (spec §18.1 — thresholds are a dial, not a ratchet)."""
    changed = 0
    async with get_session_factory()() as session:
        intent_ids = list(
            (
                await session.execute(
                    select(Delivery.intent_id)
                    .where(Delivery.intent_id.isnot(None), Delivery.feedback.isnot(None))
                    .distinct()
                )
            ).scalars()
        )
    for intent_id in intent_ids:
        if intent_id is None:
            continue
        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        select(Delivery)
                        .where(
                            Delivery.intent_id == intent_id,
                            Delivery.feedback.isnot(None),
                        )
                        .order_by(Delivery.created_at.desc())
                        .limit(LEARN_WINDOW)
                    )
                ).scalars()
            )
            intent = await session.get(StandingIntent, intent_id)
        if intent is None or len(rows) < LEARN_MIN_SAMPLE:
            continue
        mean = _mean_reward(rows)
        if mean is None:
            continue
        current = int((intent.budget or {}).get("min_urgency", 2))
        if mean <= DEMOTE_BELOW and current < MIN_URGENCY_CAP:
            target = current + 1
            verb = "raising"
        elif mean >= PROMOTE_ABOVE and len(rows) >= PROMOTE_MIN_SAMPLE and current > 2:
            target = current - 1  # recovery — floor at the default of 2
            verb = "lowering"
        else:
            continue
        reason = (
            f"watch '{intent.text[:60]}' mean reward {mean:+.2f} over {len(rows)} — "
            f"{verb} judge bar min_urgency={target}"
        )
        category = f"intent:{intent_id}:min_urgency"
        if mode == "auto":
            async with get_session_factory()() as session:
                fresh = await session.get(StandingIntent, intent_id)
                if fresh is not None:
                    fresh.budget = {**(fresh.budget or {}), "min_urgency": target}
                    await session.commit()
            await apply_policy(
                category=category, tier_override=None, reason=reason, source="learner"
            )
        else:
            await _queue_proposal(category, None, reason)
        changed += 1
    return changed


async def run_learner(now: datetime | None = None, force: bool = False) -> int:
    """One learner pass. Returns the number of adjustments applied or
    proposed. Off mode: never runs — byte-identical collection only."""
    global _last_run
    from app.registry_cache import get_cache

    mode = str(await get_cache().setting("ambient_learning_mode"))
    if mode not in {"auto", "propose"}:
        return 0
    now = now or datetime.now(UTC)
    if not force and _last_run is not None and (now - _last_run).total_seconds() < _INTERVAL_S:
        return 0
    _last_run = now
    changed = 0
    changed += await _learn_retiers(mode)
    changed += await _learn_digest_times(mode)
    changed += await _learn_intent_thresholds(mode)
    if changed:
        logger.info("ambient_learner", tier="ambient", kind="deliver", mode=mode, changes=changed)
    return changed
