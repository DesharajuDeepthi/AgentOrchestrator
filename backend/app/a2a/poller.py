"""Parked-task poller (spec §19.6) — the ambient leader-tick evaluator.

Runs inside the leader branch of the ambient loop (so it never runs N
times per replica set) and only while `a2a_enabled` is on. Each pass
rechecks parked, undelivered rows via `tasks/get`:

- terminal states become §18.4 outbox deliveries — category `a2a`,
  tier 2 (tier 1 + urgency 4 for failures), skey `a2a:{row id}` — with
  the FENCED result as the body; no recheck run is ever created (the
  `hitl_aged` "parked thing becomes a delivery" precedent);
- `input-required` becomes a tier-1 delivery carrying the fenced
  question, and the row flips to `input-required` so the Remote Agents
  task drawer takes over (reply/cancel, §8.10) — the poller leaves it
  alone until a reply re-parks it.

Recheck failures are recorded on the row and retried next tick; a row
whose agent is gone is closed out.
"""

from uuid import UUID

import structlog

from app.a2a import client_port, tasks
from app.a2a.fence import fence_remote_output, live_fence_cap
from app.db import get_session_factory
from app.models import A2A_TERMINAL_STATES, RemoteAgent

logger = structlog.get_logger("a2a")

# `a2a_poll_interval_s` watermark (spec §3.7, M40): the leader tick calls
# the poller every tick, but a pass only RUNS once the configured interval
# has elapsed since the last pass — effective cadence max(tick, interval).
# Monotonic so wall-clock jumps can't starve or double-run it.
_last_poll_monotonic: float | None = None


def reset_poll_watermark() -> None:
    """Test hook (and manager-restart hygiene): forget the last-poll time."""
    global _last_poll_monotonic
    _last_poll_monotonic = None


def _ops(kind: str, status: str) -> None:
    from app import obs

    obs.A2A_OPS.labels(kind=kind, status=status).inc()


async def _agent_name(agent_id: UUID) -> str | None:
    async with get_session_factory()() as db:
        row = await db.get(RemoteAgent, agent_id)
        if row is None or row.deleted_at is not None:
            return None
        return row.name


async def deliver_outcome(
    row_id: UUID,
    agent_id: UUID,
    agent_name: str,
    outcome: client_port.RemoteOutcome,
    *,
    run_id: UUID | None,
) -> None:
    """Terminal outcome → outbox row + task bookkeeping (shared with the
    task drawer's reply path)."""
    from app.ambient.deliver import add_delivery

    failed = outcome.state != "completed"
    fence_cap = await live_fence_cap()
    body = fence_remote_output(
        outcome.text or outcome.error or f"task ended {outcome.state}",
        agent_name=agent_name,
        state=outcome.state,
        max_chars=fence_cap,
    )
    await add_delivery(
        category="a2a",
        tier=1 if failed else 2,
        urgency=4 if failed else 2,
        title=f"[{agent_name}] remote task {outcome.state}",
        body=body,
        run_id=run_id,
        skey=f"a2a:{row_id}",
    )
    await tasks.update_task(
        row_id,
        state=outcome.state,
        result_text=outcome.text or None,
        error=outcome.error,
        delivered=True,
    )
    _ops("deliver", outcome.state)
    logger.info(
        "a2a_parked_delivered",
        tier="a2a",
        kind="deliver",
        task_id=str(row_id),
        state=outcome.state,
    )


async def poll_parked_tasks() -> int:
    """One leader-tick pass; returns how many rows settled."""
    import time

    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("a2a_enabled")):
        return 0
    global _last_poll_monotonic
    interval = max(int(await get_cache().setting("a2a_poll_interval_s")), 1)
    now = time.monotonic()
    if _last_poll_monotonic is not None and now - _last_poll_monotonic < interval:
        return 0
    _last_poll_monotonic = now
    settled = 0
    for row in await tasks.parked_tasks():
        if row.remote_task_id is None:
            await tasks.update_task(
                row.id, state="unknown", error="parked without a remote task id", delivered=True
            )
            continue
        agent_name = await _agent_name(row.remote_agent_id)
        if agent_name is None:
            await tasks.update_task(
                row.id, state="canceled", error="remote agent was deleted", delivered=True
            )
            continue
        try:
            outcome = await client_port.get_task_outcome(row.remote_agent_id, row.remote_task_id)
        except Exception as exc:  # recheck failure — retry next tick
            await tasks.update_task(row.id, parked=True, error=f"recheck failed: {exc}")
            _ops("poll", "recheck_failed")
            continue
        _ops("poll", outcome.state)
        if outcome.state in A2A_TERMINAL_STATES:
            await deliver_outcome(
                row.id, row.remote_agent_id, agent_name, outcome, run_id=row.run_id
            )
            settled += 1
        elif outcome.state == "input-required":
            from app.ambient.deliver import add_delivery

            question = fence_remote_output(
                outcome.question or "(no question text)",
                agent_name=agent_name,
                state="input-required",
                max_chars=await live_fence_cap(),
            )
            await add_delivery(
                category="a2a",
                tier=1,
                urgency=4,
                title=f"[{agent_name}] remote task needs your input",
                body=question
                + "\n\nReply from the Remote Agents page (task drawer) to continue it.",
                run_id=row.run_id,
                skey=f"a2a:{row.id}",
            )
            await tasks.update_task(row.id, state="input-required", question=outcome.question)
            settled += 1
        else:
            await tasks.update_task(row.id, parked=True, question=outcome.question)
    return settled
