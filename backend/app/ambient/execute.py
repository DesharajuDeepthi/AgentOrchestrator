"""The execution plane (spec §17.4 — milestone M22): a fired event becomes
an ORDINARY run — same orchestrators, same middleware, same ledger — with
trigger provenance, the routine's narrowed registry projection, per-run
budgets, and the abstain instruction. A supervisor task heartbeats the run
(H3), enforces budgets, and does the post-run bookkeeping: deliveries into
the outbox, consecutive-failure accounting with auto-pause, and the Letta
self-wake on failure. The reaper rescues orphans no supervisor owns.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import and_, func, or_, select

from app.db import get_session_factory
from app.models import (
    AmbientEvent,
    AmbientWakeup,
    Conversation,
    Delivery,
    Routine,
    Run,
    RunStep,
    StandingIntent,
)

logger = structlog.get_logger("ambient")

DEFAULT_BUDGETS: dict[str, Any] = {
    "max_steps": 40,
    "max_tokens": 200_000,
    "wall_clock_s": 900,
    "tokens_without_progress": 30_000,
}
_TERMINAL = {"completed", "failed", "cancelled", "stalled"}


def _first_line(text: str, limit: int = 200) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


async def prepare_run(event: AmbientEvent) -> Run | None:
    """Build the run for a fired event: fresh conversation, trusted prompt +
    untrusted-fenced payload + abstain instruction, trigger provenance set
    BEFORE the task starts (the runner reads it for the projection)."""
    from app.orchestrator.runner import create_run
    from app.prompts import load_prompt

    decision = event.decision or {}
    payload_json = json.dumps(event.payload or {}, default=str)[:4000]
    routine: Routine | None = None
    intent: StandingIntent | None = None

    if event.routine_id is not None and decision.get("fired_for") == "routine":
        async with get_session_factory()() as session:
            routine = await session.get(Routine, event.routine_id)
        if routine is None or routine.status != "active":
            logger.info(
                "ambient_execute_skipped",
                tier="ambient",
                kind="fire",
                reason="routine missing or not active",
            )
            return None
        name = routine.name
        prompt = load_prompt("ambient_run").format(
            routine_name=routine.name,
            routine_prompt=routine.prompt,
            autonomy=routine.autonomy,
            event_kind=event.kind,
            event_source=event.source,
            event_payload=payload_json,
        )
    elif decision.get("fired_for") == "intent":
        intent_id = event.intent_id or UUID(str(decision.get("intent_id")))
        async with get_session_factory()() as session:
            intent = await session.get(StandingIntent, intent_id)
        if intent is None or intent.status != "active":
            return None
        name = f"watch: {intent.text[:50]}"
        prompt = load_prompt("ambient_intent_run").format(
            intent_text=intent.text,
            event_kind=event.kind,
            event_source=event.source,
            event_payload=payload_json,
        )
    else:
        return None

    # §18.2 cross-fire continuity: an include_memories routine reuses ONE
    # persistent conversation, so history and rollups accrue across fires
    include_memories = bool(routine is not None and routine.include_memories)
    conversation_id = None
    if include_memories and routine is not None:
        async with get_session_factory()() as session:
            owner = await session.get(Routine, routine.id)
            if owner is not None and owner.conversation_id is not None:
                existing = await session.get(Conversation, owner.conversation_id)
                if existing is not None:
                    conversation_id = existing.id
    if conversation_id is None:
        async with get_session_factory()() as session:
            conversation = Conversation(
                title=f"[ambient] {name}"[:80],
                # §18.8: the ambient conversation belongs to the fire's owner
                user_id=(routine.user_id if routine else None)
                or (intent.user_id if intent else None),
            )
            session.add(conversation)
            await session.commit()
            conversation_id = conversation.id
        if include_memories and routine is not None:
            async with get_session_factory()() as session:
                owner = await session.get(Routine, routine.id)
                if owner is not None:
                    owner.conversation_id = conversation_id
                    await session.commit()
    # §18.8: a routine fires runs AS ITS OWNER; intent fires as the watch owner
    owner_id = (routine.user_id if routine else None) or (intent.user_id if intent else None)
    run = await create_run(
        conversation_id, prompt, include_memories=include_memories, user_id=owner_id
    )
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        if row is None:  # pragma: no cover - just created
            return None
        row.trigger = {
            "routine_id": str(routine.id) if routine else None,
            "intent_id": str(intent.id) if intent else None,
            "event_id": str(event.id),
            "source": event.source,
            "kind": event.kind,
            "urgency": decision.get("urgency", 2),
            # §18.1: the routine's model override, honored by the runner
            "model_ref": routine.model_ref if routine else None,
        }
        row.last_heartbeat_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
        return row


async def _cancel_run(run_id: UUID, reason: str) -> None:
    from app.orchestrator.runner import RUNNING_TASKS

    task = RUNNING_TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=10)
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        if run is not None:
            if run.status not in _TERMINAL:
                run.status = "cancelled"
                run.finished_at = datetime.now(UTC)
            run.error = f"ambient budget exceeded: {reason}"
            await session.commit()
    from app import obs

    obs.AMBIENT_OPS.labels(kind="fire", status="budget_cancelled").inc()
    logger.warning("ambient_budget_cancel", tier="ambient", kind="fire", reason=reason)


async def _supervise(run_id: UUID, budgets: dict[str, Any], poll_s: float = 15.0) -> str:
    """Heartbeat the run each poll (H3) and enforce the §17.4 budgets:
    wall-clock, max steps, max tokens, and tokens-without-progress. Returns
    the run's terminal status."""
    started = datetime.now(UTC)
    last_steps = -1
    tokens_at_progress = 0
    while True:
        await asyncio.sleep(poll_s)
        async with get_session_factory()() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return "failed"
            if run.status in _TERMINAL:
                return run.status
            run.last_heartbeat_at = datetime.now(UTC)
            await session.commit()
            if run.status == "paused_hitl":
                continue  # HITL pauses are not stalls; aging is swept separately
            tokens = run.total_input_tokens + run.total_output_tokens
            steps = (
                await session.execute(select(func.count()).where(RunStep.run_id == run_id))
            ).scalar_one()
        if steps != last_steps:
            last_steps = steps
            tokens_at_progress = tokens
        elapsed = (datetime.now(UTC) - started).total_seconds()
        breach: str | None = None
        if elapsed > float(budgets.get("wall_clock_s", DEFAULT_BUDGETS["wall_clock_s"])):
            breach = f"wall_clock_s ({int(elapsed)}s elapsed)"
        elif steps > int(budgets.get("max_steps", DEFAULT_BUDGETS["max_steps"])):
            breach = f"max_steps ({steps} steps)"
        elif tokens > int(budgets.get("max_tokens", DEFAULT_BUDGETS["max_tokens"])):
            breach = f"max_tokens ({tokens} tokens)"
        elif tokens - tokens_at_progress > int(
            budgets.get("tokens_without_progress", DEFAULT_BUDGETS["tokens_without_progress"])
        ):
            breach = f"tokens_without_progress ({tokens - tokens_at_progress} tokens, no new steps)"
        if breach is not None:
            await _cancel_run(run_id, breach)
            return "cancelled"


async def finish_ambient_run(run_id: UUID, event_id: UUID) -> None:
    """Post-run bookkeeping: outbox delivery, failure accounting + auto-pause
    (§17.6), and the Letta self-wake with the error in context. Idempotence:
    the self-wake dedupes on run_id; deliveries dedupe on (run, category)."""
    from app.registry_cache import get_cache

    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        event = await session.get(AmbientEvent, event_id)
        if run is None or event is None:
            return
        routine = (
            await session.get(Routine, event.routine_id) if event.routine_id is not None else None
        )
        intent_id = event.intent_id
        if intent_id is None and (event.decision or {}).get("intent_id"):
            intent_id = UUID(str((event.decision or {})["intent_id"]))
        intent = await session.get(StandingIntent, intent_id) if intent_id is not None else None
        existing = list(
            (await session.execute(select(Delivery).where(Delivery.run_id == run_id))).scalars()
        )
    name = routine.name if routine is not None else (intent.text[:60] if intent else "ambient")
    urgency = int((event.decision or {}).get("urgency", 2))
    now = datetime.now(UTC)

    if run.status == "completed":
        if routine is not None:
            async with get_session_factory()() as session:
                row = await session.get(Routine, routine.id)
                if row is not None and row.consecutive_failures:
                    row.consecutive_failures = 0
                    await session.commit()
        if any(d.category in {"routine", "watch", "abstained"} for d in existing):
            return
        answer = (run.final_answer or "").strip()
        abstained = answer.upper().startswith("ABSTAIN")
        if abstained:
            tier, category = 3, "abstained"
        elif intent is not None:
            threshold = int(await get_cache().setting("ambient_interrupt_threshold"))
            pref = intent.delivery
            tier = 1 if (pref == "interrupt" or (pref == "auto" and urgency >= threshold)) else 2
            category = "watch"
        else:
            tier, category = 2, "routine"
        from app.ambient.deliver import add_delivery

        # supersede-collapse (spec §17.5): repeated fires of the same
        # routine/watch replace their still-pending predecessor
        skey = (
            f"intent:{intent.id}:{category}"
            if intent is not None
            else (f"routine:{routine.id}:{category}" if routine is not None else None)
        )
        await add_delivery(
            run_id=run_id,
            intent_id=intent.id if intent is not None else None,
            category=category,
            tier=tier,
            urgency=urgency,
            title=f"[{name}] {_first_line(answer) or '(no answer)'}",
            body=answer or None,
            skey=skey,
            deliver_no_later_than=now + timedelta(minutes=30) if tier == 1 else None,
        )
        logger.info(
            "ambient_run_finished",
            tier="ambient",
            kind="deliver",
            run_id=str(run_id),
            delivery_tier=tier,
            abstained=abstained,
        )
        return

    if run.status in {"failed", "stalled", "cancelled"}:
        paused = False
        if routine is not None:
            async with get_session_factory()() as session:
                row = await session.get(Routine, routine.id)
                if row is not None:
                    row.consecutive_failures += 1
                    if row.consecutive_failures >= 3 and row.status == "active":
                        row.status = "paused"
                        row.status_reason = (
                            f"auto-paused after 3 consecutive failures "
                            f"(last: {_first_line(run.error or run.status, 120)})"
                        )
                        paused = True
                    await session.commit()
            # Letta pattern: one immediate self-wake with the error in
            # context instead of dying silently — deduped per failed run
            from app.ambient.wakeups import WakeupCapError, schedule_wakeup

            async with get_session_factory()() as session:
                already = (
                    await session.execute(
                        select(func.count()).where(AmbientWakeup.run_id == run_id)
                    )
                ).scalar_one()
            # scheduled even when this failure auto-paused the routine: the
            # decision plane drops fires into paused routines, and the
            # ledger then shows exactly why nothing ran
            if not already:
                try:
                    await schedule_wakeup(
                        routine.id,
                        delay_s=60,
                        reason=f"self-wake after failure: {_first_line(run.error or '?', 200)}",
                        payload={"failed_run_id": str(run_id), "error": (run.error or "")[:500]},
                        created_by="system",
                        run_id=run_id,
                    )
                except WakeupCapError as exc:
                    logger.warning("ambient_selfwake_capped", error=str(exc))
        if any(d.category == "failure" for d in existing):
            return
        from app.ambient.deliver import add_delivery

        await add_delivery(
            run_id=run_id,
            intent_id=intent.id if intent is not None else None,
            category="failure",
            tier=0 if paused else 1,
            urgency=5 if paused else 4,
            title=(
                f"[{name}] auto-paused after repeated failures"
                if paused
                else f"[{name}] run {run.status}: {_first_line(run.error or '', 120)}"
            ),
            body=run.error,
            deliver_no_later_than=now + timedelta(minutes=30),
        )
        from app import obs

        obs.AMBIENT_OPS.labels(kind="fire", status=run.status).inc()


async def execute_fired_event(event_id: UUID, poll_s: float = 15.0) -> UUID | None:
    """Tier 3 (spec §17.3): the run. Prepare → start → supervise → book-keep.
    Never raises — the drain's executor tasks must not die loudly."""
    try:
        async with get_session_factory()() as session:
            event = await session.get(AmbientEvent, event_id)
        if event is None or event.verdict != "fired":
            return None
        run = await prepare_run(event)
        if run is None:
            return None
        budgets = dict(DEFAULT_BUDGETS)
        if event.routine_id is not None:
            async with get_session_factory()() as session:
                routine = await session.get(Routine, event.routine_id)
            if routine is not None and routine.budgets:
                budgets.update(routine.budgets)
        from app import obs
        from app.orchestrator.runner import start_run_task

        start_run_task(run.id)
        obs.AMBIENT_OPS.labels(kind="fire", status="started").inc()
        logger.info(
            "ambient_run_started",
            tier="ambient",
            kind="fire",
            run_id=str(run.id),
            event_id=str(event_id),
        )
        await _supervise(run.id, budgets, poll_s=poll_s)
        await finish_ambient_run(run.id, event_id)
        return run.id
    except Exception as exc:  # noqa: BLE001 — executor tasks never die loudly
        logger.exception("ambient_execute_failed", event_id=str(event_id), error=str(exc))
        return None


async def reap_stalled_runs(now: datetime | None = None, stall_after_s: int | None = None) -> int:
    """H3 reaper: ambient runs whose heartbeat went silent are marked
    stalled, their task (if any) cancelled, and the owning routine paused
    with a visible reason (spec §17.4). Returns runs reaped. The window is
    the live `run_stall_after_s` setting (M40) unless a test passes one."""
    from app.orchestrator.runner import RUNNING_TASKS

    if stall_after_s is None:
        from app.registry_cache import get_cache

        stall_after_s = max(int(await get_cache().setting("run_stall_after_s")), 60)
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=stall_after_s)
    async with get_session_factory()() as session:
        stale = list(
            (
                await session.execute(
                    select(Run).where(
                        Run.trigger.isnot(None),
                        Run.status == "running",
                        or_(
                            Run.last_heartbeat_at <= cutoff,
                            and_(Run.last_heartbeat_at.is_(None), Run.started_at <= cutoff),
                        ),
                    )
                )
            ).scalars()
        )
        for run in stale:
            run.status = "stalled"
            run.error = f"stalled: no heartbeat for over {stall_after_s}s"
            run.finished_at = now
        await session.commit()
    for run in stale:
        task = RUNNING_TASKS.get(run.id)
        if task is not None and not task.done():
            task.cancel()
        routine_id = (run.trigger or {}).get("routine_id")
        if routine_id:
            async with get_session_factory()() as session:
                routine = await session.get(Routine, UUID(str(routine_id)))
                if routine is not None and routine.status == "active":
                    routine.status = "paused"
                    routine.status_reason = f"auto-paused: run {run.id} stalled (no heartbeat)"
                    await session.commit()
        from app import obs

        obs.AMBIENT_OPS.labels(kind="stall", status="reaped").inc()
        logger.warning("ambient_run_stalled", tier="ambient", kind="stall", run_id=str(run.id))
    return len(stale)
