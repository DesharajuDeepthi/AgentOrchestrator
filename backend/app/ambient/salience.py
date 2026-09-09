"""Delivery salience (spec §17.5 — milestone M42).

Tier and urgency are declared *a priori* by whoever emitted the delivery;
nothing re-judges an alert on the merits of what it actually says. This
pass closes that gap for the one case where it matters most: a delivery
that was supposed to reach a human in real time and **did not**.

Two stages, cheap first:

1. a deterministic prefilter — urgency floor, real-time tier, and the
   **recurrence count over the row's `skey` lineage** (a thing that keeps
   coming back is evidence in itself; the system already collapsed those
   repeats and never read them as a signal);
2. an LLM-as-judge over the FENCED body returning
   `{verdict, reason, confidence}`.

Three outcomes, each written to the row's `salience` for the §17.6 ledger:

- **escalate** — lead the next digest. Never a re-interrupt: salience may
  raise an item's place in a queue the delivery plane already chose, never
  re-open a delivery decision or break quiet hours.
- **retain** — hand the content to §16 with delivery provenance, so a fact
  survives even though the notification carrying it was never read.
- **drop** — recorded explicitly; silence stays an explicit, logged decision.

The judge is advisory and **fail-open** (the §4 overlap-guard precedent):
on any error the row keeps exactly the disposition it already had.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_session_factory
from app.llm import ModelParams, get_model
from app.models import Delivery
from app.prompts import load_prompt

logger = structlog.get_logger("ambient")

# how much fenced content the judge sees; the fence itself must never fail open
_MAX_JUDGED_CHARS = 4000


class SalienceVerdict(BaseModel):
    """Judge output for one unseen delivery."""

    verdict: Literal["escalate", "retain", "drop"] = "drop"
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def fence_delivery_content(body: str, *, category: str, urgency: int, recurrence: int) -> str:
    """Delivered content is untrusted — remote-agent output reaches
    deliveries through §19.6 — so it is fenced before entering any model
    context, exactly like a remote tool result."""
    return (
        load_prompt("delivery_salience")
        .replace("{category}", category.replace('"', "'"))
        .replace("{urgency}", str(urgency))
        .replace("{recurrence}", str(recurrence))
        .replace("{content}", (body or "").strip()[:_MAX_JUDGED_CHARS] or "(empty)")
    )


async def _recurrence(row: Delivery) -> int:
    """How many deliveries share this row's `skey` lineage (M42: the signal
    supersede-collapse has been throwing away since M23)."""
    if not row.skey:
        return 1
    async with get_session_factory()() as session:
        rows = list(
            (await session.execute(select(Delivery.id).where(Delivery.skey == row.skey))).scalars()
        )
    return max(len(rows), 1)


async def prefilter(row: Delivery, min_urgency: int) -> tuple[bool, str]:
    """Deterministic gate before a model is ever called. Returns
    (is_candidate, why-not)."""
    if row.tier > 1:
        return False, "digest/silent tiers are not real-time — an empty room is normal"
    if row.seen_at is not None:
        return False, "already seen"
    if row.superseded_by is not None:
        return False, "superseded by a newer row"
    if row.delivered_at is None:
        return False, "never delivered — nothing to re-judge"
    recurrence = await _recurrence(row)
    # a recurring alert clears the urgency floor on persistence alone
    if row.urgency < min_urgency and recurrence < 3:
        return False, f"urgency {row.urgency} below floor {min_urgency} and not recurring"
    return True, ""


async def judge(row: Delivery) -> SalienceVerdict | None:
    """Ask the model. Returns None when the judge is unavailable — the
    caller must then leave the row exactly as it found it."""
    from app.factory.worker import resolve_node_model
    from app.registry_cache import get_cache

    recurrence = await _recurrence(row)
    prompt = fence_delivery_content(
        row.body or row.title,
        category=row.category,
        urgency=row.urgency,
        recurrence=recurrence,
    )
    try:
        override = await get_cache().setting("ambient_salience_model")
        model_ref, model_params = await resolve_node_model({}, {})
        if override:
            # an explicit salience model overrides the node default, params
            # and all (spec §3.7: nullable, null falls back to default_model)
            raw = await get_cache().setting("ambient_salience_model_params")
            model_ref = str(override)
            model_params = ModelParams.model_validate(raw) if raw else None
        model = get_model(model_ref, model_params)
        structured = model.with_structured_output(SalienceVerdict)
        verdict = await structured.ainvoke(prompt)
        assert isinstance(verdict, SalienceVerdict)
        return verdict
    except Exception as exc:  # noqa: BLE001 — advisory: never blocks the outbox
        logger.warning("salience_judge_unavailable", tier="ambient", kind="deliver", error=str(exc))
        return None


async def _perform(
    row_id: UUID, verdict: str, *, by_human: bool
) -> tuple[dict[str, Any], list[str]]:
    """Carry out one verdict and return everything undo will need: the state
    as it stood immediately BEFORE the mutation, and the ids of the memories
    retention itself created (never the ones the run already had)."""
    prior: dict[str, Any] = {}
    async with get_session_factory()() as session:
        row = await session.get(Delivery, row_id)
        if row is None:
            return {}, []
        prior = {
            "tier": row.tier,
            "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
            "channel": row.channel,
            "seen_at": row.seen_at.isoformat() if row.seen_at else None,
        }
        if verdict == "escalate":
            # digest-lead ONLY: never a re-interrupt, never a tier < 2, so
            # quiet hours and the budget stay untouched (spec §17.5)
            row.tier = 2
            row.delivered_at = None
            row.channel = None
        elif verdict == "drop" and by_human and row.seen_at is None:
            # a human dismissing the proposal is the ONLY thing that marks a
            # delivery seen — the pass never does it on the user's behalf
            row.seen_at = datetime.now(UTC)
        await session.commit()
    memory_ids = await _retain(row_id) if verdict == "retain" else []
    return prior, memory_ids


async def _apply(row_id: UUID, verdict: SalienceVerdict, mode: str) -> str:
    """Write the outcome. In `propose` the verdict is recorded but NOT
    applied — it queues for the §8.9 approval control (M43)."""
    applied = mode == "auto"
    prior: dict[str, Any] = {}
    memory_ids: list[str] = []
    if applied:
        prior, memory_ids = await _perform(row_id, verdict.verdict, by_human=False)
    async with get_session_factory()() as session:
        row = await session.get(Delivery, row_id)
        if row is None:
            return "gone"
        row.salience = {
            "verdict": verdict.verdict,
            "reason": verdict.reason[:500],
            "confidence": verdict.confidence,
            "at": datetime.now(UTC).isoformat(),
            "mode": mode,
            "applied": applied,
            # M43 undo provenance — present only once something was applied
            "decision": "applied" if applied else None,
            "decided_at": datetime.now(UTC).isoformat() if applied else None,
            "decided_by": "system" if applied else None,
            "prior": prior or None,
            "memory_ids": memory_ids,
        }
        await session.commit()
    logger.info(
        "ambient_salience_verdict",
        tier="ambient",
        kind="deliver",
        delivery_id=str(row_id),
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        mode=mode,
        applied=applied,
    )
    return verdict.verdict


async def _retain(row_id: UUID) -> list[str]:
    """Hand the content to §16 through the normal admission path, carrying
    delivery provenance. Memory off ⇒ no-op, like every other §16 caller.
    Returns the ids of the memories THIS call created, so undo can retract
    exactly those and nothing the run already held (M43)."""
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("memory_enabled")):
        return []
    async with get_session_factory()() as session:
        row = await session.get(Delivery, row_id)
    if row is None or row.run_id is None:
        # provenance-free content has no run to attribute; the verdict is
        # still ledgered, but nothing is written to memory
        return []
    try:
        from app.memory.extract import extract_from_run

        created = await extract_from_run(row.run_id)
        return [str(m.id) for m in created or []]
    except Exception as exc:  # noqa: BLE001 — retention must never block
        logger.warning("salience_retain_failed", delivery_id=str(row_id), error=str(exc))
        return []


# ── the decision surface (spec §17.5/§8.9 — M43) ──────────────────────────

Action = Literal["apply", "decline", "undo"]
# what each action needs to find on the row, and what it leaves behind
_TERMINAL = {"applied", "declined", "undone"}


async def decide(row_id: UUID, action: Action) -> tuple[str, str]:
    """Act on a recorded verdict. Returns (outcome, detail) where outcome is
    one of ok | noop | conflict | missing — the caller maps those to status
    codes. First decision wins: replaying one is a no-op, contradicting one
    is refused, so a double-clicked button can never double-apply."""
    async with get_session_factory()() as session:
        row = await session.get(Delivery, row_id)
        if row is None:
            return "missing", "no such delivery"
        ledger = dict(row.salience or {})
    if not ledger:
        return "conflict", "no salience verdict on this delivery"
    verdict = str(ledger.get("verdict") or "")
    decision = ledger.get("decision")

    if action == "apply":
        if decision == "applied":
            return "noop", "already applied"
        if decision in _TERMINAL:
            return "conflict", f"already {decision}"
        prior, memory_ids = await _perform(row_id, verdict, by_human=True)
        ledger |= {"applied": True, "prior": prior or None, "memory_ids": memory_ids}
        await _close(row_id, ledger, "applied")
        return "ok", verdict

    if action == "decline":
        if decision == "declined":
            return "noop", "already declined"
        if decision in _TERMINAL:
            return "conflict", f"already {decision}"
        # a decline changes NO state — it only says the verdict was wrong
        await _close(row_id, ledger, "declined")
        return "ok", verdict

    if decision != "applied":
        return "conflict", "nothing applied to undo"
    prior = dict(ledger.get("prior") or {})
    async with get_session_factory()() as session:
        row = await session.get(Delivery, row_id)
        if row is None:
            return "missing", "no such delivery"
        if verdict == "escalate":
            if row.delivered_at is not None:
                # the digest already went out — the mutation is spent, and
                # pretending otherwise would be a lie (spec §17.5)
                return "conflict", "already delivered in a digest — the escalation is spent"
            row.tier = int(prior.get("tier", row.tier))
            row.channel = prior.get("channel")
            row.delivered_at = _parse(prior.get("delivered_at"))
        elif verdict == "drop":
            row.seen_at = _parse(prior.get("seen_at"))
        await session.commit()
    if verdict == "retain":
        from app.memory.store import hard_delete

        for mid in ledger.get("memory_ids") or []:
            try:
                await hard_delete(UUID(str(mid)))
            except Exception as exc:  # noqa: BLE001 — a gone memory is still undone
                logger.warning("salience_undo_memory_failed", memory_id=str(mid), error=str(exc))
    await _close(row_id, ledger, "undone")
    return "ok", verdict


def _parse(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


# the judge's own track record: apply endorses the verdict, decline and
# undo repudiate it. This is a reward for the JUDGE and lives on the
# salience record — never on the delivery. "The judge misread this alert"
# and "this alert was worthless" are different facts: writing dismissed
# onto a real-but-mishandled alert would feed §17.3 precision and could
# demote the alert's whole category for the judge's mistake.
_JUDGE_REWARD = {"applied": 1.0, "declined": -1.0, "undone": -1.0}


async def _close(row_id: UUID, ledger: dict[str, Any], decision: str) -> None:
    """Stamp the decision and the judge's reward onto the ledger.
    Append-only in spirit: the verdict, reason and confidence that
    produced it are never rewritten."""
    async with get_session_factory()() as session:
        row = await session.get(Delivery, row_id)
        if row is None:
            return
        row.salience = ledger | {
            "decision": decision,
            "decided_at": datetime.now(UTC).isoformat(),
            "decided_by": "user",
            "applied": decision == "applied",
            "judge_reward": _JUDGE_REWARD[decision],
        }
        await session.commit()
    logger.info(
        "ambient_salience_decision",
        tier="ambient",
        kind="deliver",
        delivery_id=str(row_id),
        decision=decision,
        verdict=ledger.get("verdict"),
        judge_reward=_JUDGE_REWARD[decision],
    )


async def run_salience_pass(limit: int = 20) -> dict[str, int]:
    """One tick's pass over unseen real-time deliveries (spec §17.5)."""
    from app.registry_cache import get_cache

    out = {"considered": 0, "judged": 0, "escalate": 0, "retain": 0, "drop": 0, "skipped": 0}
    mode = str(await get_cache().setting("ambient_salience_mode") or "off")
    if mode == "off":
        return out
    min_urgency = int(await get_cache().setting("ambient_salience_min_urgency") or 3)
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(Delivery)
                    .where(
                        Delivery.tier <= 1,
                        Delivery.seen_at.is_(None),
                        Delivery.superseded_by.is_(None),
                        Delivery.delivered_at.isnot(None),
                        Delivery.salience.is_(None),  # judged once
                    )
                    .order_by(Delivery.urgency.desc(), Delivery.created_at)
                    .limit(limit)
                )
            ).scalars()
        )
    for row in rows:
        out["considered"] += 1
        from app.ambient.salience_learn import salience_muted

        if await salience_muted(row.category, row.user_id):
            out["skipped"] += 1  # FLE-3: the learner muted this category
            continue
        ok, why = await prefilter(row, min_urgency)
        if not ok:
            out["skipped"] += 1
            logger.debug("salience_prefiltered", delivery_id=str(row.id), reason=why)
            continue
        verdict = await judge(row)
        if verdict is None:  # fail-open: leave the row exactly as found
            out["skipped"] += 1
            continue
        out["judged"] += 1
        result = await _apply(row.id, verdict, mode)
        if result in out:
            out[result] += 1
    return out
