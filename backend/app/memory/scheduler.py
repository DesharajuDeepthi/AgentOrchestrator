"""Consolidation scheduler (spec §16.2) — single-process asyncio.

M14 ships the post-run pipeline (digest → rollup; M15 chains extraction) and
the advisory-lock helper periodic jobs (M17) reuse. Post-run work is
fire-and-forget from the runner: when `memory_enabled` is off the spawned
task reads one cached setting and exits — no writes, no behavior change.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger("memory")

_TASKS: set[asyncio.Task[None]] = set()
_POST_RUN_DEBOUNCE_S = 1.0
# advisory-lock namespace (spec §16.2): classid fixed, objid per job class
_LOCK_CLASSID = 42016

# M15 extraction + M16 procedural learning; M17 adds periodic jobs;
# M18 adds citation feedback (§16.7)
from app.memory.extract import post_run_extract  # noqa: E402
from app.memory.feedback import post_run_citation  # noqa: E402
from app.memory.procedural import post_run_procedural  # noqa: E402

POST_RUN_EXTRA: list[Callable[[UUID], Awaitable[object]]] = [
    post_run_extract,
    post_run_procedural,
    post_run_citation,
]


def on_run_completed(run_id: UUID) -> None:
    """Runner hook — fire-and-forget; never blocks or fails the run."""
    try:
        task = asyncio.create_task(_post_run(run_id))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except RuntimeError:  # pragma: no cover - no loop (sync test contexts)
        pass


async def process_run(run_id: UUID) -> None:
    """The post-run pipeline, awaitable directly (tests, backfills)."""
    from app.db import get_session_factory
    from app.memory.episodic import digest_run, update_rollup
    from app.models import Run

    digest = await digest_run(run_id)
    if digest is not None:
        await update_rollup(digest.conversation_id)
    else:
        async with get_session_factory()() as session:
            run = await session.get(Run, run_id)
        if run is not None:
            await update_rollup(run.conversation_id)
    for job in list(POST_RUN_EXTRA):
        try:
            await job(run_id)
        except Exception as exc:  # noqa: BLE001 — jobs are independent
            logger.warning("memory_post_run_job_failed", job=job.__name__, error=str(exc))


async def _post_run(run_id: UUID) -> None:
    try:
        from app.registry_cache import get_cache

        if not await get_cache().setting("memory_enabled"):
            return
        await asyncio.sleep(_POST_RUN_DEBOUNCE_S)
        await process_run(run_id)
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        raise
    except Exception as exc:  # noqa: BLE001 — consolidation never crashes the app
        logger.warning("memory_post_run_failed", run_id=str(run_id), error=str(exc))


async def acquire_job_lock(session: Any, job_id: int) -> bool:
    """pg_try_advisory_lock so exactly one replica runs a periodic job class.
    Session-level: the caller holds the SESSION open for the pass and the lock
    releases with the connection (spec §16.2 / research 04 §5)."""
    from sqlalchemy import text as sql_text

    row = await session.execute(
        sql_text("SELECT pg_try_advisory_lock(:classid, :objid)"),
        {"classid": _LOCK_CLASSID, "objid": job_id},
    )
    return bool(row.scalar())


async def release_job_lock(session: Any, job_id: int) -> None:
    from sqlalchemy import text as sql_text

    await session.execute(
        sql_text("SELECT pg_advisory_unlock(:classid, :objid)"),
        {"classid": _LOCK_CLASSID, "objid": job_id},
    )


async def drain() -> None:
    """Await all in-flight post-run tasks (tests + graceful shutdown)."""
    if _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)


def shutdown() -> None:
    for task in list(_TASKS):
        task.cancel()
