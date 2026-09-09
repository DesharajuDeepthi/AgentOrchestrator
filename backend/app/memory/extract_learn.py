"""Extraction tuner (M47): the second §17.7 feedback consumer — the
tombstone-informed learner the M44 spec text promised ("any future
learner over them enters under the §17.7 feedback-consumer rule").
Own gate (`memory_extraction_learning`, off|propose|auto, default off,
born dark), hard clamps, deterministic rules — no bandit: forgets and
review rejections are rare events (the FLE-1 §2.3 decision).

Inputs — machine writes only (`MACHINE_SOURCES`); the human's own words
are consent, never a training signal:
- tombstones of machine-written memories (M44) with their
  confidence-at-admission metadata,
- quarantine review rejections (`status='rejected'`).

Two adjustments, both riding the EXISTING policy ledger so reject and
the proposal review UI work with no new machinery:

- **kind routing** — a kind whose machine writes are chronically
  repudiated (≥ ROUTE_RATE over ≥ MIN_REPUDIATED) is added to
  `memory_quarantine_kinds`: future machine writes of that kind land in
  the §16.2 review queue instead of activating directly. Novel junk of
  a repudiated kind is exactly what the M44 forget gate cannot catch —
  tombstones suppress repeats, not new facts.
- **admission-floor moves** — `memory_admission_min_confidence` ±0.05,
  clamped to [FLOOR_MIN, FLOOR_MAX], at most one move per invocation.

Rule refinement vs the research doc, forced by the harness (recorded
here and in the experiment report, the M45 precedent): the doc said the
floor moves "when a kind's forget-rate is persistently high" — but a
raw rate ratchets the floor against confidence-INDEPENDENT repudiation
until it starves valuable kinds. The shipped trigger is band-local: the
floor rises only when the band a +0.05 bump would newly refuse
([floor, floor+0.05)) is itself mostly repudiated — the tombstones'
confidence-at-admission metadata makes this measurable, which is what
"tombstone-informed" means. It relaxes back toward the default only on
a clean stream. Undo story: the setting stays a first-class Settings
field — editing it there IS the human override, per the M43 settings-
completeness rule.
"""

import structlog
from sqlalchemy import select

from app.db import get_session_factory
from app.models import Memory, MemoryTombstone

logger = structlog.get_logger("memory")

MIN_REPUDIATED = 5  # repudiations before any action on a kind
ROUTE_RATE = 0.5  # repudiated share that sends a kind through review
FLOOR_STEP = 0.05
FLOOR_MIN, FLOOR_MAX = 0.5, 0.9
BAND_JUNK_SHARE = 0.6  # the next band must be mostly junk to raise
CLEAN_RATE = 0.05  # relax toward default only under this
CLEAN_MIN_SAMPLE = 10


async def run_extraction_tuner(force: bool = False) -> dict[str, int]:
    """One tuner pass over the tombstone + review-rejection ledger."""
    from app.ambient.learn import apply_policy
    from app.memory.store import MACHINE_SOURCES
    from app.registry_cache import get_cache
    from app.settings_store import update_settings

    out = {"considered": 0, "kind_routes": 0, "floor_moves": 0}
    mode = str(await get_cache().setting("memory_extraction_learning") or "off")
    if mode == "off":
        return out
    source = "learner" if mode == "auto" else "learner_proposal"

    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(select(Memory).where(Memory.source.in_(MACHINE_SOURCES)))
            ).scalars()
        )
        stones = list(
            (
                await session.execute(
                    select(MemoryTombstone).where(MemoryTombstone.source.in_(MACHINE_SOURCES))
                )
            ).scalars()
        )

    # per-kind ledger: kept = active rows; repudiated = tombstones (the
    # human forgot it) + rejected reviews (the human refused it)
    kept: dict[str, list[float]] = {}
    repudiated: dict[str, list[float]] = {}
    admitted: dict[str, int] = {}
    for row in rows:
        admitted[row.kind] = admitted.get(row.kind, 0) + 1
        if row.status == "active":
            kept.setdefault(row.kind, []).append(float(row.confidence))
        elif row.status == "rejected":
            repudiated.setdefault(row.kind, []).append(float(row.confidence))
    for stone in stones:
        admitted[stone.kind] = admitted.get(stone.kind, 0) + 1
        repudiated.setdefault(stone.kind, []).append(float(stone.confidence or 0.0))
    out["considered"] = sum(admitted.values())

    # 1) kind routing
    routed = set(await get_cache().setting("memory_quarantine_kinds") or [])
    for kind, total in admitted.items():
        bad = len(repudiated.get(kind, []))
        if kind in routed or bad < MIN_REPUDIATED or bad / total < ROUTE_RATE:
            continue
        reason = f"extraction learner: {bad}/{total} machine writes repudiated || proposed=+{kind}"
        if mode == "auto":
            async with get_session_factory()() as session:
                await update_settings(session, {"memory_quarantine_kinds": sorted(routed | {kind})})
            routed.add(kind)
        await apply_policy(
            category="setting:memory_quarantine_kinds",
            tier_override=None,
            reason=reason,
            source=source,
        )
        out["kind_routes"] += 1
        logger.info(
            "extraction_learner_route",
            tier="memory",
            kind="extract",
            memory_kind=kind,
            repudiated=bad,
            admitted=total,
            mode=mode,
        )

    # 2) one floor move per invocation, judged over the un-routed kinds
    floor = float(await get_cache().setting("memory_admission_min_confidence") or FLOOR_MIN)
    band_hi = round(floor + FLOOR_STEP, 2)
    band_bad = band_good = all_bad = all_total = 0
    for kind in admitted:
        if kind in routed:
            continue
        bad_confs = repudiated.get(kind, [])
        good_confs = kept.get(kind, [])
        all_bad += len(bad_confs)
        all_total += len(bad_confs) + len(good_confs)
        band_bad += sum(1 for c in bad_confs if floor <= c < band_hi)
        band_good += sum(1 for c in good_confs if floor <= c < band_hi)
    target = None
    reason = ""
    if (
        band_bad >= MIN_REPUDIATED
        and band_bad / max(band_bad + band_good, 1) >= BAND_JUNK_SHARE
        and floor < FLOOR_MAX
    ):
        target = round(min(floor + FLOOR_STEP, FLOOR_MAX), 2)
        reason = (
            f"extraction learner: band [{floor:.2f},{band_hi:.2f}) is "
            f"{band_bad}/{band_bad + band_good} repudiated || proposed={target:.2f}"
        )
    elif all_total >= CLEAN_MIN_SAMPLE and all_bad / all_total <= CLEAN_RATE and floor > FLOOR_MIN:
        target = round(max(floor - FLOOR_STEP, FLOOR_MIN), 2)
        reason = (
            f"extraction learner: clean stream ({all_bad}/{all_total} repudiated) "
            f"|| proposed={target:.2f}"
        )
    if target is not None:
        if mode == "auto":
            async with get_session_factory()() as session:
                await update_settings(session, {"memory_admission_min_confidence": target})
        await apply_policy(
            category="setting:memory_admission_min_confidence",
            tier_override=None,
            reason=reason,
            source=source,
        )
        out["floor_moves"] += 1
        logger.info(
            "extraction_learner_floor",
            tier="memory",
            kind="extract",
            floor=floor,
            proposed=target,
            mode=mode,
        )
    return out
