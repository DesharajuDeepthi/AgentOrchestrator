"""The ambient drain (spec §17.2): NOTIFY wakes it, the table is the truth.

Pending events are claimed with FOR UPDATE SKIP LOCKED and handed to the
registered processor. M20 ships the substrate: the default processor leaves
events pending (the decision plane lands in M21) — the drain's job here is
liveness, ordering, and the wake path. The loop is lifespan-owned and a
no-op while `ambient_enabled` is false (byte-identity when dark).
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.ambient.store import NOTIFY_CHANNEL
from app.db import get_session_factory
from app.models import AmbientEvent

logger = structlog.get_logger("ambient")

# processor contract: return (verdict, reason) or (verdict, reason, decision)
# or None to leave pending. Processors MUST NOT write ambient_events rows —
# the drain holds them FOR UPDATE and applies the outcome itself.
ProcessorResult = tuple[str, str] | tuple[str, str, dict[str, "object"]] | None
Processor = Callable[[AmbientEvent], Awaitable[ProcessorResult]]
_processor: Processor | None = None

# executor contract (spec §17.4): called AFTER the drain commits a 'fired'
# verdict whose decision names an addressee — fire-and-forget, own session,
# never while event rows are held FOR UPDATE.
Executor = Callable[..., Coroutine[Any, Any, object]]
_executor: Executor | None = None
_EXEC_TASKS: set[asyncio.Task[object]] = set()


def register_processor(fn: Processor | None) -> None:
    """The loop installs the decision plane here; tests install fakes."""
    global _processor
    _processor = fn


def register_executor(fn: Executor | None) -> None:
    """The loop installs the execution plane here; tests install fakes."""
    global _executor
    _executor = fn


async def default_processor(event: AmbientEvent) -> ProcessorResult:
    """M21 decision plane: patterns advance first (derived events), then the
    three-tier gate decides fire/hold for this event."""
    from app.ambient.decide import process_event
    from app.ambient.patterns import advance_patterns

    await advance_patterns(event)
    return await process_event(event)


async def drain_once(limit: int = 20) -> int:
    """Claim and process pending events. Returns events handled."""
    handled = 0
    to_execute: list[Any] = []
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    sql_text(
                        """
                        SELECT id FROM ambient_events
                        WHERE verdict IS NULL
                        ORDER BY received_at
                        LIMIT :n
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"n": limit},
                )
            )
            .scalars()
            .all()
        )
        for event_id in rows:
            event = await session.get(AmbientEvent, event_id)
            if event is None:
                continue
            outcome: ProcessorResult = None
            if _processor is not None:
                try:
                    outcome = await _processor(event)
                except Exception as exc:  # noqa: BLE001 — the drain never dies
                    outcome = ("held", f"processor error: {exc}")
            if outcome is not None:
                event.verdict = outcome[0]
                event.verdict_reason = outcome[1]
                if len(outcome) == 3:
                    event.decision = outcome[2]
                event.processed_at = datetime.now(UTC)
                handled += 1
                if (
                    outcome[0] == "fired"
                    and len(outcome) == 3
                    and dict(outcome[2]).get("fired_for")
                ):
                    to_execute.append(event.id)
        await session.commit()
    # tier 3 — the run (spec §17.4): launched only after the rows are
    # committed and released, never while the drain holds them FOR UPDATE
    if _executor is not None:
        for event_id in to_execute:
            task: asyncio.Task[object] = asyncio.create_task(_executor(event_id))
            _EXEC_TASKS.add(task)
            task.add_done_callback(_EXEC_TASKS.discard)
    if handled:
        from app import obs

        obs.AMBIENT_OPS.labels(kind="drain", status="ok").inc()
        logger.info("ambient_drain", tier="ambient", kind="drain", handled=handled)
    return handled


async def run_ambient_loop(stop: asyncio.Event, tick_s: float | None = None) -> None:
    """Lifespan-owned loop: LISTEN for wake pings, sweep on the tick.
    Cheap no-op while ambient is dark. Under `--scale backend=N` the tick
    elects a leader per tick (spec §18.9): only the leader runs the
    evaluators; every replica LISTENs and drains (SKIP-LOCKED-safe)."""
    from app.ambient.coordinate import LeaderLease
    from app.registry_cache import get_cache

    wake = asyncio.Event()
    listener_task: asyncio.Task[None] | None = None
    lease = LeaderLease()

    async def _listen() -> None:
        # dedicated (unpooled) connection: LISTEN breaks under pooling
        import asyncpg  # type: ignore[import-untyped]

        from app.config import get_config

        dsn = get_config().database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        try:
            await conn.add_listener(NOTIFY_CHANNEL, lambda *_: wake.set())
            await stop.wait()
        finally:
            await conn.close()

    while not stop.is_set():
        try:
            enabled = bool(await get_cache().setting("ambient_enabled"))
            if enabled:
                if listener_task is None or listener_task.done():
                    listener_task = asyncio.create_task(_listen())
                if _processor is None:
                    register_processor(default_processor)
                if _executor is None:
                    from app.ambient.execute import execute_fired_event

                    register_executor(execute_fired_event)
                from app import obs

                # §18.9 leader election: the session advisory lock is the
                # lease — renew-or-acquire once per tick, failover ≤ one tick
                leader = await lease.ensure()
                obs.AMBIENT_LEADER.set(1.0 if leader else 0.0)
                if leader:
                    # trigger evaluators (spec §17.2) then the drain
                    from app.ambient.decide import sweep_hitl_aging
                    from app.ambient.execute import reap_stalled_runs
                    from app.ambient.patterns import expire_pattern_deadlines
                    from app.ambient.triggers import (
                        evaluate_schedules,
                        evaluate_state_conditions,
                        poll_due_intents,
                    )
                    from app.ambient.wakeups import fire_due_wakeups

                    await evaluate_schedules()
                    await poll_due_intents()
                    await evaluate_state_conditions()
                    await expire_pattern_deadlines()
                    await fire_due_wakeups()
                    await sweep_hitl_aging()
                    await reap_stalled_runs()
                    await drain_once()
                    idle_minutes = int(await get_cache().setting("ambient_idle_minutes"))
                    from app.ambient.deliver import flush_deliveries
                    from app.ambient.presence import evaluate_presence, is_platform_idle

                    await evaluate_presence(idle_minutes)
                    await flush_deliveries()
                    # M42 §17.5: re-judge what nobody saw. Runs AFTER the
                    # flush so this tick's misses are already visible, and
                    # is a no-op while ambient_salience_mode is off
                    from app.ambient.salience import run_salience_pass
                    from app.ambient.salience_learn import run_salience_tuner

                    await run_salience_pass()
                    await run_salience_tuner()  # FLE-3: no-op unless gated on
                    if await is_platform_idle(idle_minutes):
                        from app.ambient.anticipate import run_anticipation

                        await run_anticipation()
                    # §17.7 learner — consolidation-class, throttled internally,
                    # a no-op unless ambient_learning_mode is auto|propose
                    from app.ambient.learn import run_learner

                    await run_learner()
                    # §19.6 parked A2A tasks — leader-only recheck; a no-op
                    # while a2a_enabled is off
                    from app.a2a.poller import poll_parked_tasks

                    await poll_parked_tasks()
                else:
                    # non-leaders LISTEN + drain only (spec §18.9): the
                    # SKIP-LOCKED drain and the executor are replica-safe
                    await drain_once()
            elif lease.held:
                # ambient went dark — surrender leadership immediately
                from app import obs

                await lease.release()
                obs.AMBIENT_LEADER.set(0.0)
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            logger.warning("ambient_tick_failed", error=str(exc))
        wake.clear()
        # M40: the tick cadence is a live setting (an explicit tick_s arg —
        # tests — still wins); floor 15s matches the §3.7 validation bound
        if tick_s is not None:
            effective_tick = tick_s
        else:
            try:
                effective_tick = float(
                    max(int(await get_cache().setting("ambient_tick_interval_s")), 15)
                )
            except Exception:  # noqa: BLE001 — a settings hiccup must not stop the loop
                effective_tick = 60.0
        waiters = {asyncio.create_task(stop.wait()), asyncio.create_task(wake.wait())}
        _, pending = await asyncio.wait(
            waiters, timeout=effective_tick, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    # a clean stop releases the lease NOW; a crash lapses it with the session
    await lease.release()
    if listener_task is not None:
        listener_task.cancel()
