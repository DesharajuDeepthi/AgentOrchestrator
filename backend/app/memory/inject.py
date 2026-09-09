"""Injection plane (spec §16.3) — the budgeted "remembered context" block.

Assembled at prompt-assembly time (never middleware — §7.0 stays at three
custom middlewares). Fenced as DATA with a fixed abstention line; every
injection emits §16.6 observability with counts and token cost. Fail-open:
any error returns an empty block and the run proceeds memory-blind.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger("memory")

_CHARS_PER_TOKEN = 4  # budget approximation, consistent across surfaces


@dataclass
class InjectionStats:
    surface: str
    pinned: int = 0
    memories: int = 0
    episodes: int = 0
    communities: int = 0  # §18.6 community breadth
    tokens: int = 0
    memory_ids: list[str] = field(default_factory=list)


def _clip(lines: list[str], budget_tokens: int) -> list[str]:
    out: list[str] = []
    used = 0
    for line in lines:
        cost = max(len(line) // _CHARS_PER_TOKEN, 1)
        if used + cost > budget_tokens:
            break
        out.append(line)
        used += cost
    return out


async def build_memory_block(
    query: str,
    *,
    conversation_id: UUID | None,
    surface: str,
) -> tuple[str, InjectionStats]:
    """The complete remembered-context block for one surface, or ("", stats)
    when memory is off, nothing clears the floor, or anything fails."""
    stats = InjectionStats(surface=surface)
    try:
        from app.registry_cache import get_cache

        cache = get_cache()
        if not await cache.setting("memory_enabled"):
            return "", stats
        budget = int(await cache.setting("memory_injection_budget_tokens"))
        pinned_budget = int(await cache.setting("memory_pinned_budget_tokens"))
        top_k = int(await cache.setting("memory_recall_top_k"))
        floor = float(await cache.setting("memory_score_floor"))

        from app.memory.episodic import recall_digests
        from app.memory.rank import pinned_memories, recall

        # §18.2: project rows inject only into their project's conversations
        project_key: str | None = None
        if conversation_id is not None:
            from app.db import get_session_factory
            from app.models import Conversation

            async with get_session_factory()() as session:
                conv = await session.get(Conversation, conversation_id)
                project_key = conv.project_key if conv is not None else None

        pinned = await pinned_memories(conversation_id)
        pinned_lines = _clip(
            [f"- [{m.kind} {str(m.id)[:8]}] {m.text}" for m in pinned], pinned_budget
        )

        # §16.7 citation feedback: injection does NOT bump access — being
        # retrieved is not evidence of being useful; the post-run citation
        # job reinforces only memories the answer actually cited
        hits = await recall(
            query,
            conversation_id=conversation_id,
            project_key=project_key,
            k=top_k,
            floor=floor,
            bump_access=False,
        )
        pinned_ids = {m.id for m in pinned}
        # approved standing instructions get their own labeled section: they
        # earned activation through review (or explicit user statement) and,
        # unlike remembered facts, are meant to steer behavior (spec §16.3)
        instr_lines = [
            f"- [{str(h.memory.id)[:8]}] {h.memory.text}"
            for h in hits
            if h.memory.kind == "instruction" and h.memory.id not in pinned_ids
        ]
        mem_lines = [
            f"- [{h.memory.kind} {str(h.memory.id)[:8]} score={h.score:.2f}] {h.memory.text}"
            for h in hits
            if h.memory.id not in pinned_ids and h.memory.kind != "instruction"
        ]

        episodes = await recall_digests(query, k=3, exclude_conversation_id=conversation_id)
        epi_lines = [f"- [episode {str(d.id)[:8]} score={s:.2f}] {d.text}" for d, s in episodes]

        # §18.6 community breadth: a recalled entity's community summary is
        # eligible under its OWN budget line — never per-query graph work
        community_budget = int(await cache.setting("memory_community_budget_tokens"))
        com_lines: list[str] = []
        if community_budget > 0 and hits:
            from app.memory.communities import communities_for_memories

            found = await communities_for_memories([h.memory.id for h in hits])
            com_lines = _clip(
                [
                    f"- [community {c.label[:8]} · {c.member_count} entities] {c.summary}"
                    for c in found
                    if c.summary
                ],
                community_budget,
            )

        # spend the main budget on instructions, then memories, then episodes
        instr_lines = _clip(instr_lines, budget)
        used = sum(max(len(x) // _CHARS_PER_TOKEN, 1) for x in instr_lines)
        mem_lines = _clip(mem_lines, max(budget - used, 0))
        used += sum(max(len(x) // _CHARS_PER_TOKEN, 1) for x in mem_lines)
        epi_lines = _clip(epi_lines, max(budget - used, 0))

        if (
            not pinned_lines
            and not instr_lines
            and not mem_lines
            and not epi_lines
            and not com_lines
        ):
            return "", stats

        from app.prompts import load_prompt

        def section(title: str, lines: list[str]) -> str:
            return f"\n{title}:\n" + "\n".join(lines) + "\n" if lines else ""

        block = load_prompt("memory_block").format(
            pinned_section=section("Pinned profile", pinned_lines),
            instructions_section=section(
                "Approved standing instructions (the user approved these — follow them "
                "unless the current request overrides)",
                instr_lines,
            ),
            memories_section=section("Relevant memories", mem_lines),
            episodes_section=section("Similar past episodes (other conversations)", epi_lines),
            communities_section=section("Community context (related-entity summaries)", com_lines),
        )
        stats.pinned = len(pinned_lines)
        stats.memories = len(mem_lines)
        stats.episodes = len(epi_lines)
        stats.communities = len(com_lines)
        stats.tokens = max(len(block) // _CHARS_PER_TOKEN, 1)
        stats.memory_ids = [str(h.memory.id) for h in hits] + [str(m.id) for m in pinned]
        _record_injected(stats.memory_ids)
        _observe(stats)
        return block, stats
    except Exception as exc:  # noqa: BLE001 — memory never breaks a run
        logger.warning("memory_inject_failed", surface=surface, error=str(exc))
        return "", stats


def _record_injected(memory_ids: list[str]) -> None:
    """Note injected ids on the run context for §16.7 citation feedback."""
    try:
        from app.orchestrator.context import get_run_context

        ctx = get_run_context()
        if ctx is not None:
            known = set(ctx.injected_memory_ids)
            ctx.injected_memory_ids.extend(i for i in memory_ids if i not in known)
    except Exception:  # noqa: BLE001, S110 - bookkeeping only
        pass


def _observe(stats: InjectionStats) -> None:
    from app import obs

    obs.MEMORY_INJECTED_TOKENS.labels(surface=stats.surface).observe(stats.tokens)
    obs.MEMORY_OPS.labels(kind="inject", status="ok").inc()
    logger.info(
        "memory_injected",
        communities=stats.communities,
        tier="memory",
        kind="inject",
        surface=stats.surface,
        pinned=stats.pinned,
        memories=stats.memories,
        episodes=stats.episodes,
        tokens=stats.tokens,
    )
    # the live run trace shows the injection (spec §16.6)
    try:
        from app.orchestrator.context import get_run_context

        ctx = get_run_context()
        if ctx is not None:
            ctx.recorder.emit(
                "activity",
                {
                    "label": (
                        f"memory: injected {stats.memories} memories, "
                        f"{stats.episodes} episodes (~{stats.tokens} tok) into {stats.surface}"
                    )
                },
            )
    except Exception:  # noqa: BLE001, S110 - trace decoration only
        pass


def emitted_stats_payload(stats: InjectionStats) -> dict[str, Any]:
    return {
        "surface": stats.surface,
        "pinned": stats.pinned,
        "memories": stats.memories,
        "episodes": stats.episodes,
        "tokens": stats.tokens,
    }
