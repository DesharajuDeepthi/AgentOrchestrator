"""Ambient delivery/watch/ledger API (spec §17.5/§17.6 + §8.9 — M23).

Inbox = the deliveries outbox; feedback capture computes and persists the
blended reward. Watches list standing intents with their compiled rules and
cadence state. Ledger exposes the fire/hold audit with per-category
intervention precision.
"""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import SessionDep
from app.auth import current_user_id, owns_row, scope_to_user
from app.db import get_session_factory
from app.models import AmbientEvent, AmbientPolicy, Delivery, StandingIntent

deliveries_router = APIRouter(prefix="/deliveries", tags=["ambient"])
watches_router = APIRouter(prefix="/watches", tags=["ambient"])
ledger_router = APIRouter(prefix="/ambient", tags=["ambient"])


def _delivery_out(d: Delivery) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "run_id": str(d.run_id) if d.run_id else None,
        "intent_id": str(d.intent_id) if d.intent_id else None,
        "category": d.category,
        "tier": d.tier,
        "urgency": d.urgency,
        "title": d.title,
        "body": d.body,
        "channel": d.channel,
        "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
        "deliver_no_later_than": (
            d.deliver_no_later_than.isoformat() if d.deliver_no_later_than else None
        ),
        "superseded_by": str(d.superseded_by) if d.superseded_by else None,
        "feedback": d.feedback,
        "reward": d.reward,
        "external": d.external,  # §18.4 per-channel send ledger
        "seen_at": d.seen_at.isoformat() if d.seen_at else None,  # M42
        "salience": d.salience,  # §17.5 judge verdict, M42
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@deliveries_router.get("")
async def list_deliveries(
    session: SessionDep, status: str = "all", limit: int = 100
) -> dict[str, Any]:
    query = scope_to_user(
        select(Delivery).order_by(Delivery.created_at.desc()).limit(min(limit, 500)), Delivery
    )
    if status == "pending":
        query = query.where(Delivery.delivered_at.is_(None), Delivery.superseded_by.is_(None))
    elif status == "delivered":
        query = query.where(Delivery.delivered_at.isnot(None))
    rows = list((await session.execute(query)).scalars())
    return {"items": [_delivery_out(d) for d in rows]}


@deliveries_router.get("/unread-count")
async def unread_count(session: SessionDep) -> dict[str, int]:
    """M42: delivered-but-never-opened items — the Ambient nav badge. Only
    what was actually delivered counts; pending rows are not yet news."""
    rows = list(
        (
            await session.execute(
                scope_to_user(select(Delivery), Delivery).where(
                    Delivery.delivered_at.isnot(None),
                    Delivery.seen_at.is_(None),
                    Delivery.superseded_by.is_(None),
                )
            )
        ).scalars()
    )
    return {"count": len(rows), "attention": len([r for r in rows if r.tier <= 1])}


@deliveries_router.post("/{delivery_id}/seen")
async def mark_seen(delivery_id: UUID, session: SessionDep) -> dict[str, Any]:
    """M42: opening an item in the Inbox stamps seen_at — this is what turns
    'was it attended to' from an inference into a fact (§18.4)."""
    row = await session.get(Delivery, delivery_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "no such delivery")
    if row.seen_at is None:  # idempotent: first open wins
        row.seen_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
    return _delivery_out(row)


@deliveries_router.post("/{delivery_id}/salience/{action}")
async def decide_salience(delivery_id: UUID, action: str) -> dict[str, Any]:
    """M43: act on a §17.5 verdict — apply it, decline it, or undo one that
    was applied (by a human or by `auto`). First decision wins; a repeat is
    a no-op, a contradiction is refused, and an escalation already spent by
    a digest refuses honestly instead of pretending to reverse."""
    from app.ambient.salience import Action, decide

    if action not in ("apply", "decline", "undo"):
        raise HTTPException(422, "action must be one of ['apply', 'decline', 'undo']")
    async with get_session_factory()() as session:
        existing = await session.get(Delivery, delivery_id)
    if existing is None or not owns_row(existing):
        raise HTTPException(404, "no such delivery")
    outcome, detail = await decide(delivery_id, cast("Action", action))
    if outcome == "missing":
        raise HTTPException(404, detail)
    if outcome == "conflict":
        raise HTTPException(409, detail)
    async with get_session_factory()() as session:
        row = await session.get(Delivery, delivery_id)
    if row is None:
        raise HTTPException(404, "no such delivery")
    return _delivery_out(row) | {"outcome": outcome, "detail": detail}


@deliveries_router.get("/digest-preview")
async def digest_preview(session: SessionDep) -> dict[str, Any]:
    """What the next digest flush would contain, in flush order."""
    rows = list(
        (
            await session.execute(
                scope_to_user(select(Delivery), Delivery)
                .where(
                    Delivery.tier == 2,
                    Delivery.delivered_at.is_(None),
                    Delivery.superseded_by.is_(None),
                )
                .order_by(Delivery.urgency.desc(), Delivery.created_at)
            )
        ).scalars()
    )
    return {"items": [_delivery_out(d) for d in rows]}


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: str


@deliveries_router.post("/{delivery_id}/feedback")
async def give_feedback(delivery_id: UUID, body: FeedbackBody) -> dict[str, Any]:
    from app.ambient.deliver import FEEDBACK_VALUES, record_feedback

    if body.feedback not in FEEDBACK_VALUES:
        raise HTTPException(422, f"feedback must be one of {sorted(FEEDBACK_VALUES)}")
    async with get_session_factory()() as session:
        existing = await session.get(Delivery, delivery_id)
    if existing is None or not owns_row(existing):
        raise HTTPException(404, "no such delivery")
    row = await record_feedback(delivery_id, body.feedback)
    if row is None:
        raise HTTPException(404, "no such delivery")
    return _delivery_out(row)


def _watch_out(w: StandingIntent) -> dict[str, Any]:
    return {
        "id": str(w.id),
        "text": w.text,
        "condition_type": w.condition_type,
        "compiled": w.compiled,
        "semantic_predicate": w.semantic_predicate,
        "judge_model_ref": w.judge_model_ref,
        "watermark": w.watermark,
        "cadence": {
            "base_interval_s": w.base_interval_s,
            "current_interval_s": w.current_interval_s,
            "max_interval_s": w.max_interval_s,
            "consecutive_quiet": w.consecutive_quiet,
            "last_checked_at": w.last_checked_at.isoformat() if w.last_checked_at else None,
        },
        "expires_at": w.expires_at.isoformat() if w.expires_at else None,
        "delivery": w.delivery,
        "status": w.status,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


@watches_router.get("")
async def list_watches(session: SessionDep, limit: int = 100) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                scope_to_user(select(StandingIntent), StandingIntent)
                .order_by(StandingIntent.created_at.desc())
                .limit(min(limit, 500))
            )
        ).scalars()
    )
    return {"items": [_watch_out(w) for w in rows]}


class WatchPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class WatchCompileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class WatchFilterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: str = "equals"
    value: str = ""
    values: list[str] = []


class WatchCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    filters: list[WatchFilterBody] = []
    semantic_predicate: str | None = None
    cadence_s: int = 300


async def _require_ambient_on() -> None:
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("ambient_enabled")):
        raise HTTPException(409, "ambient mode is disabled (ambient_enabled=false)")


@watches_router.post("/compile")
async def compile_watch(body: WatchCompileBody) -> dict[str, Any]:
    """§18.5 page authoring: NL → typed rule via the SAME compiler the
    `ambient.watch` tool uses; the row stays 'proposed' until the user
    confirms (PATCH status=active)."""
    from app.ambient.watch_compile import compile_and_propose

    await _require_ambient_on()
    out = await compile_and_propose(body.text)
    if out["status"] == "rejected":
        raise HTTPException(422, str(out.get("error")))
    return out


@watches_router.post("", status_code=201)
async def create_watch(body: WatchCreateBody, session: SessionDep) -> dict[str, Any]:
    """§18.5: a typed event-filter watch built directly from filter rows —
    no compiler involved; still lands 'proposed' for an explicit confirm."""
    from app.ambient.watch_compile import VALID_FILTER_OPS

    await _require_ambient_on()
    if not body.filters:
        raise HTTPException(422, "an events watch needs at least one filter")
    for f in body.filters:
        if f.op not in VALID_FILTER_OPS:
            raise HTTPException(422, f"unknown filter op {f.op!r} — use {sorted(VALID_FILTER_OPS)}")
    row = StandingIntent(
        user_id=current_user_id(),
        text=body.text[:2000],
        condition_type="event",
        compiled={"match": "events", "filters": [f.model_dump() for f in body.filters]},
        semantic_predicate=body.semantic_predicate,
        base_interval_s=max(body.cadence_s, 60),
        current_interval_s=max(body.cadence_s, 60),
        status="proposed",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _watch_out(row)


@watches_router.patch("/{intent_id}")
async def patch_watch(intent_id: UUID, body: WatchPatch, session: SessionDep) -> dict[str, Any]:
    if body.status not in {"active", "paused", "retired"}:
        raise HTTPException(422, "status must be active | paused | retired")
    row = await session.get(StandingIntent, intent_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "no such watch")
    if row.status == "proposed" and body.status == "active":
        pass  # confirming a proposal from the UI is allowed
    row.status = body.status
    await session.commit()
    await session.refresh(row)
    return _watch_out(row)


@ledger_router.get("/stream")
async def ambient_stream() -> Any:
    """Global delivery-event SSE (spec §18.4): exists only while ambient is
    on — 409 when dark, mirroring the fire endpoint. The UI subscribes only
    when the settings snapshot says ambient is on; dark ⇒ no stream, no
    subscription, no toast."""
    import asyncio

    from fastapi.responses import StreamingResponse

    from app.ambient import channels
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("ambient_enabled")):
        raise HTTPException(409, "ambient mode is disabled (ambient_enabled=false)")

    async def gen() -> Any:
        sub_id, queue = channels.subscribe_stream()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=channels.STREAM_KEEPALIVE_S)
                except TimeoutError:
                    # keepalive doubles as the dark check: the stream exists
                    # only while ambient is on
                    if not bool(await get_cache().setting("ambient_enabled")):
                        return
                    yield ": keepalive\n\n"
                    continue
                yield channels.sse_line(event)
        finally:
            channels.unsubscribe_stream(sub_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@ledger_router.get("/ledger")
async def ledger(
    session: SessionDep,
    limit: int = 100,
    verdict: str = "all",
    correlation_id: UUID | None = None,
) -> dict[str, Any]:
    if correlation_id is not None:
        # §18.5 correlation-chain view: the whole chain, cause → effect
        query = (
            select(AmbientEvent)
            .where(AmbientEvent.correlation_id == correlation_id)
            .order_by(AmbientEvent.depth, AmbientEvent.occurred_at)
        )
    else:
        query = (
            select(AmbientEvent).order_by(AmbientEvent.received_at.desc()).limit(min(limit, 500))
        )
        if verdict in {"fired", "held", "dropped", "expired"}:
            query = query.where(AmbientEvent.verdict == verdict)
        elif verdict == "pending":
            query = query.where(AmbientEvent.verdict.is_(None))
    rows = list((await session.execute(query)).scalars())
    return {
        "items": [
            {
                "id": str(e.id),
                "kind": e.kind,
                "source": e.source,
                "routine_id": str(e.routine_id) if e.routine_id else None,
                "intent_id": str(e.intent_id) if e.intent_id else None,
                "verdict": e.verdict,
                "verdict_reason": e.verdict_reason,
                "decision": e.decision,
                "causation_id": str(e.causation_id) if e.causation_id else None,
                "correlation_id": str(e.correlation_id) if e.correlation_id else None,
                "depth": e.depth,
                "received_at": e.received_at.isoformat() if e.received_at else None,
                "processed_at": e.processed_at.isoformat() if e.processed_at else None,
            }
            for e in rows
        ]
    }


@ledger_router.get("/precision")
async def precision(session: SessionDep) -> dict[str, Any]:
    """Per-category intervention precision + the active policy override."""
    from app.ambient.deliver import category_precision

    categories = list((await session.execute(select(Delivery.category).distinct())).scalars())
    policies: dict[str, AmbientPolicy] = {}
    for p in (
        (
            await session.execute(
                select(AmbientPolicy)
                .where(
                    AmbientPolicy.source.notin_(["learner_proposal", "learner_rejected"])
                )  # inert
                .order_by(AmbientPolicy.created_at)
            )
        )
        .scalars()
        .all()
    ):
        policies[p.category] = p  # latest applied wins
    from app.ambient.deliver import PRECISION_WINDOW

    out = []
    for cat in sorted(categories):
        prec, n = await category_precision(cat)
        policy = policies.get(cat)
        # §18.5 sparkline: the judged window, chronological, accepted=1
        judged_rows = list(
            (
                await session.execute(
                    select(Delivery.feedback)
                    .where(
                        Delivery.category == cat,
                        Delivery.feedback.in_(["accepted", "dismissed"]),
                    )
                    .order_by(Delivery.created_at.desc())
                    .limit(PRECISION_WINDOW)
                )
            ).scalars()
        )
        series = [1 if f == "accepted" else 0 for f in reversed(judged_rows)]
        out.append(
            {
                "category": cat,
                "precision": prec,
                "judged": n,
                "series": series,
                "tier_override": policy.tier_override if policy else None,
                "override_reason": policy.reason if policy else None,
                "override_source": policy.source if policy else None,
            }
        )
    return {"items": out}


@ledger_router.get("/policies")
async def policies(session: SessionDep, limit: int = 100) -> dict[str, Any]:
    """The append-only policy ledger, newest first (§17.6 audit + revert)."""
    rows = list(
        (
            await session.execute(
                select(AmbientPolicy)
                .order_by(AmbientPolicy.created_at.desc())
                .limit(min(limit, 500))
            )
        ).scalars()
    )
    return {
        "items": [
            {
                "id": str(p.id),
                "category": p.category,
                "tier_override": p.tier_override,
                "reason": p.reason,
                "source": p.source,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ]
    }


class PolicyRevertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str


@ledger_router.post("/policies/{policy_id}/approve")
async def approve_policy(policy_id: UUID) -> dict[str, Any]:
    """Approve a queued learner proposal (spec §17.7 propose mode)."""
    from app.ambient.learn import approve_proposal

    row = await approve_proposal(policy_id)
    if row is None:
        raise HTTPException(404, "no learner proposal with that id")
    return {
        "id": str(row.id),
        "category": row.category,
        "tier_override": row.tier_override,
        "status": "applied",
    }


@ledger_router.post("/policies/{policy_id}/reject")
async def reject_policy(policy_id: UUID) -> dict[str, Any]:
    """M44 §17.7: reject a queued learner proposal — captured, never applied."""
    from app.ambient.learn import reject_proposal

    row = await reject_proposal(policy_id)
    if row is None:
        raise HTTPException(404, "no learner proposal with that id")
    return {"id": str(row.id), "category": row.category, "status": "rejected"}


@ledger_router.post("/policies/revert")
async def revert_policy(body: PolicyRevertBody, session: SessionDep) -> dict[str, Any]:
    """One-click revert (spec §17.7): append a clearing row for the
    category — history stays, the override stops applying."""
    row = AmbientPolicy(
        category=body.category,
        tier_override=None,
        reason="reverted by user",
        source="user",
    )
    session.add(row)
    await session.commit()
    return {"category": body.category, "tier_override": None, "status": "reverted"}
