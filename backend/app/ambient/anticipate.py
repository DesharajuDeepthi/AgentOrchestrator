"""The anticipation job (spec §17.4 idle work — milestone M23).

When the platform goes idle, predict the user's likely next asks from
recent activity + standing watches, pre-compute a short briefing, and queue
it as ONE tier-2 digest item per idle window. Every briefing is scored by
its feedback; below the hit-rate floor the job self-prunes (stops running)
until the category earns its way back.
"""

from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_session_factory
from app.models import Delivery, Run, StandingIntent

logger = structlog.get_logger("ambient")

HIT_RATE_FLOOR = 0.2
HIT_RATE_MIN_SAMPLE = 5
MAX_ITEMS = 3
_RECENT_RUNS = 5


class AnticipationItem(BaseModel):
    title: str
    note: str


class AnticipationOutput(BaseModel):
    items: list[AnticipationItem] = Field(default_factory=list)


async def hit_rate_allows() -> bool:
    """Self-prune (spec §17.4): once ≥5 briefings are judged and fewer than
    HIT_RATE_FLOOR were accepted, stop producing them."""
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(Delivery.feedback)
                    .where(
                        Delivery.category == "anticipation",
                        Delivery.feedback.isnot(None),
                    )
                    .order_by(Delivery.created_at.desc())
                    .limit(20)
                )
            ).scalars()
        )
    if len(rows) < HIT_RATE_MIN_SAMPLE:
        return True
    return rows.count("accepted") / len(rows) >= HIT_RATE_FLOOR


async def run_anticipation(now: datetime | None = None) -> list[UUID] | None:
    """One briefing per idle window (deduped by skey prefix), one delivery
    PER PREDICTED ITEM (spec §18.1) so used/unused feedback and the hit-rate
    floor operate per item. Returns the created delivery ids, or None when
    gated. The caller (the ambient tick) owns the platform-idle check."""
    from app.ambient.deliver import add_delivery
    from app.llm import ModelParams, get_model
    from app.prompts import load_prompt
    from app.registry_cache import get_cache

    # M48 §3.7.1: the only feature that initiates contact unprompted, so
    # it answers to a switch — not only to the hit-rate floor learning it
    if not bool(await get_cache().setting("ambient_anticipation_enabled")):
        return None

    now = now or datetime.now(UTC)
    window_key = f"anticipation:{now.strftime('%Y%m%d%H')}"
    async with get_session_factory()() as session:
        existing = (
            (
                await session.execute(
                    select(Delivery.id).where(Delivery.skey.like(f"{window_key}%")).limit(1)
                )
            )
            .scalars()
            .first()
        )
    if existing is not None:
        return None
    if not await hit_rate_allows():
        logger.info(
            "ambient_anticipation_pruned", tier="ambient", kind="deliver", reason="hit rate"
        )
        return None

    async with get_session_factory()() as session:
        runs = list(
            (
                await session.execute(
                    select(Run)
                    .where(Run.trigger.is_(None), Run.status == "completed")
                    .order_by(Run.started_at.desc())
                    .limit(_RECENT_RUNS)
                )
            ).scalars()
        )
        watches = list(
            (
                await session.execute(
                    select(StandingIntent.text).where(StandingIntent.status == "active")
                )
            ).scalars()
        )
    if not runs:
        return None
    activity = "\n".join(
        f"- asked: {r.chat_message[:150]} — got: {(r.final_answer or '')[:150]}" for r in runs
    )
    cache = get_cache()
    ref = await cache.setting("memory_extraction_model") or await cache.setting("default_model")
    model = get_model(str(ref), ModelParams(effort="low"))
    prompt = load_prompt("ambient_anticipation").format(
        recent_activity=activity,
        watches="\n".join(f"- {w}" for w in watches) or "(none)",
    )
    try:
        out = await model.with_structured_output(AnticipationOutput).ainvoke(prompt)
        assert isinstance(out, AnticipationOutput)
    except Exception as exc:  # noqa: BLE001 — anticipation never breaks the tick
        logger.warning("ambient_anticipation_failed", error=str(exc))
        return None
    items = out.items[:MAX_ITEMS]
    if not items:
        return None
    created: list[UUID] = []
    for i, item in enumerate(items):
        delivery = await add_delivery(
            category="anticipation",
            tier=2,
            urgency=2,
            title=f"Anticipated: {item.title}"[:250],
            body=item.note,
            skey=f"{window_key}:{i}",
        )
        created.append(delivery.id)
    logger.info("ambient_anticipation", tier="ambient", kind="deliver", items=len(created))
    return created
