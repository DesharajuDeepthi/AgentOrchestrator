"""Citation feedback (spec §16.7) — used beats retrieved.

Injection-path recall no longer bumps access bookkeeping; this post-run job
matches the injected memory ids' 8-char prefixes against the run's final
answer (the injected block prints ids precisely so answers can cite them).
Cited memories get the access bump plus an importance reinforcement (+1,
capped at 10, at most once per run); injected-but-uncited memories get
nothing and cool toward decay naturally. Fail-open like all consolidation.
"""

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, update

from app.db import get_session_factory
from app.models import Memory, Run

logger = structlog.get_logger("memory")


def cited_ids(answer: str, injected: list[str]) -> list[UUID]:
    """Which injected memory ids does the answer text actually cite?"""
    out: list[UUID] = []
    seen: set[str] = set()
    for mid in injected:
        if mid in seen:
            continue
        seen.add(mid)
        if mid[:8] and mid[:8] in answer:
            out.append(UUID(mid))
    return out


async def post_run_citation(run_id: UUID) -> int:
    """Scheduler chain entry: reinforce the memories the answer cited.
    Returns the number of memories reinforced."""
    from app.orchestrator.context import get_run_context
    from app.registry_cache import get_cache

    if not await get_cache().setting("memory_enabled"):
        return 0
    ctx = get_run_context()
    injected: list[str] = list(getattr(ctx, "injected_memory_ids", []) or []) if ctx else []
    if not injected:
        return 0
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        answer = (run.final_answer or "") if run is not None else ""
        cited = cited_ids(answer, injected)
        if cited:
            await session.execute(
                update(Memory)
                .where(Memory.id.in_(cited))
                .values(
                    last_accessed_at=datetime.now(UTC),
                    access_count=Memory.access_count + 1,
                    importance=func.least(Memory.importance + 1, 10),
                )
            )
            await session.commit()
    from app import obs

    obs.MEMORY_OPS.labels(kind="cite", status="ok").inc()
    logger.info(
        "memory_citation_feedback",
        tier="memory",
        kind="cite",
        run_id=str(run_id),
        injected=len(set(injected)),
        cited=len(cited),
    )
    return len(cited)
