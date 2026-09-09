"""Memory communities (spec §18.6 — milestone M31).

The Zep-style upgrade deferred at M13: synchronous label propagation over
the entity co-occurrence graph (two entities are neighbors when they share
a memory), deterministic tie-breaks (most frequent neighbor label, ties to
the lexicographically smallest), community label = the min member entity
id. Communities are rebuilt by a consolidation-class job — never per
query — and a member-set signature keeps summaries stable: only a changed
community re-summarizes (extraction model; a summarize failure keeps the
previous summary rather than losing it). Singletons form no community;
an empty graph is a no-op.
"""

import hashlib
from collections import Counter
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from app.db import get_session_factory
from app.models import Memory, MemoryCommunity, MemoryEntity, MemoryEntityLink

logger = structlog.get_logger("memory")

_MAX_ROUNDS = 20
_SUMMARY_MEMBER_CAP = 12  # entities named in the summary prompt
_SUMMARY_MEMORY_CAP = 8  # member memories quoted in the summary prompt


def _propagate(adjacency: dict[str, set[str]]) -> dict[str, list[str]]:
    """Label propagation with deterministic order and tie-breaks.
    Returns {community label (min member id): sorted member ids}."""
    labels = {node: node for node in adjacency}
    for _ in range(_MAX_ROUNDS):
        changed = False
        for node in sorted(adjacency):
            neighbors = adjacency[node]
            if not neighbors:
                continue
            counts = Counter(labels[n] for n in neighbors)
            # ties resolve to the lexicographically smallest label
            top = max(counts.values())
            candidate = min(lbl for lbl, c in counts.items() if c == top)
            if candidate != labels[node]:
                labels[node] = candidate
                changed = True
        if not changed:
            break
    groups: dict[str, list[str]] = {}
    for node, label in labels.items():
        groups.setdefault(label, []).append(node)
    # canonical label = min member id; singletons are not communities
    return {min(members): sorted(members) for members in groups.values() if len(members) >= 2}


def _signature(members: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:64]


async def _summarize(member_ids: list[str]) -> str | None:
    """One generative summary via the extraction role; None on failure."""
    from app.memory.extract import _extraction_model
    from app.prompts import load_prompt

    try:
        async with get_session_factory()() as session:
            entities = list(
                (
                    await session.execute(
                        select(MemoryEntity).where(
                            MemoryEntity.id.in_([UUID(m) for m in member_ids])
                        )
                    )
                ).scalars()
            )
            memory_rows = list(
                (
                    await session.execute(
                        select(Memory)
                        .join(MemoryEntityLink, MemoryEntityLink.memory_id == Memory.id)
                        .where(
                            MemoryEntityLink.entity_id.in_([UUID(m) for m in member_ids]),
                            Memory.status == "active",
                        )
                        .order_by(Memory.recorded_at.desc())
                        .limit(_SUMMARY_MEMORY_CAP)
                    )
                )
                .scalars()
                .unique()
            )
        prompt = load_prompt("memory_community_summary").format(
            entities=", ".join(sorted(e.name for e in entities)[:_SUMMARY_MEMBER_CAP]),
            memories="\n".join(f"- {m.text}" for m in memory_rows) or "(none)",
        )
        _, model = await _extraction_model()
        out = await model.ainvoke(prompt)  # type: ignore[attr-defined]
        text = out.content if isinstance(out.content, str) else str(out.content)
        return str(text).strip() or None
    except Exception as exc:  # noqa: BLE001 — memory never breaks anything
        logger.warning("memory_community_summary_failed", error=str(exc))
        return None


async def rebuild_communities() -> int:
    """The §18.6 consolidation job: recompute communities, keep summaries
    for unchanged member sets, re-summarize changed ones, drop vanished
    rows. Returns the number of communities present after the pass."""
    # M48 §3.7.1: gated in-function so no call path rebuilds behind the
    # switch. Two ways off: the named gate, or — the corollary — a zero
    # budget, which READS as off and so must BE off; before M48 a zero
    # budget silenced the injected section while this job kept spending
    # one summarization call per changed community.
    from app.memory.lifecycle import JOB_COMMUNITIES, JOB_GATES, gate_open
    from app.registry_cache import get_cache

    if not await gate_open(JOB_GATES[JOB_COMMUNITIES]):
        return 0
    if int(await get_cache().setting("memory_community_budget_tokens") or 0) <= 0:
        return 0

    async with get_session_factory()() as session:
        links = list((await session.execute(select(MemoryEntityLink))).scalars())
    if not links:
        async with get_session_factory()() as session:
            for row in (await session.execute(select(MemoryCommunity))).scalars():
                await session.delete(row)
            await session.commit()
        return 0

    by_memory: dict[str, list[str]] = {}
    for link in links:
        by_memory.setdefault(str(link.memory_id), []).append(str(link.entity_id))
    adjacency: dict[str, set[str]] = {}
    for entity_ids in by_memory.values():
        for a in entity_ids:
            adjacency.setdefault(a, set())
            for b in entity_ids:
                if a != b:
                    adjacency[a].add(b)
    communities = _propagate(adjacency)

    async with get_session_factory()() as session:
        existing = {
            row.label: row for row in (await session.execute(select(MemoryCommunity))).scalars()
        }
    for label, members in communities.items():
        sig = _signature(members)
        current = existing.get(label)
        if current is not None and current.signature == sig:
            continue  # unchanged — keep the stored summary, no model call
        summary = await _summarize(members)
        async with get_session_factory()() as session:
            found_row: MemoryCommunity | None = (
                await session.execute(select(MemoryCommunity).where(MemoryCommunity.label == label))
            ).scalar_one_or_none()
            if found_row is None:
                found_row = MemoryCommunity(label=label)
                session.add(found_row)
            found_row.member_entity_ids = members
            found_row.member_count = len(members)
            found_row.signature = sig
            if summary is not None or found_row.summary is None:
                found_row.summary = summary
            await session.commit()
    # drop communities whose label vanished from the graph
    async with get_session_factory()() as session:
        for row in (await session.execute(select(MemoryCommunity))).scalars():
            if row.label not in communities:
                await session.delete(row)
        await session.commit()
    logger.info(
        "memory_communities_rebuilt",
        tier="memory",
        kind="communities",
        communities=len(communities),
    )
    return len(communities)


async def communities_for_memories(memory_ids: list[UUID]) -> list[MemoryCommunity]:
    """§18.6 recall breadth: the communities reachable from these memories'
    entities — read-only, never triggers a rebuild."""
    if not memory_ids:
        return []
    async with get_session_factory()() as session:
        entity_ids = {
            str(r[0])
            for r in (
                await session.execute(
                    select(MemoryEntityLink.entity_id).where(
                        MemoryEntityLink.memory_id.in_(memory_ids)
                    )
                )
            ).all()
        }
        if not entity_ids:
            return []
        rows = list((await session.execute(select(MemoryCommunity))).scalars())
    out: list[MemoryCommunity] = []
    for row in rows:
        members: list[Any] = row.member_entity_ids or []
        if entity_ids & {str(m) for m in members}:
            out.append(row)
    out.sort(key=lambda r: r.member_count, reverse=True)
    return out
