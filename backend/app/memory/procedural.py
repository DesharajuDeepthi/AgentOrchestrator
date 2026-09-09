"""L3 procedural learning (spec §16.5).

Consumes the episodic layer; lands in existing governance. The
experience-following caution (research 03 §5) applies throughout: only
positively-signaled runs are harvested, exemplars carry an ExpeL vote
lifecycle (upvote on reuse-success, downvote on reuse-failure, retire at
zero), and mined skill proposals pass doclint + the overlap judge and land
INACTIVE for human review — no autonomous registry mutation.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy import text as sql_text

from app.db import get_session_factory
from app.models import PlanExemplar, RoutingStat, Run, RunStep

logger = structlog.get_logger("memory")

_EXEMPLAR_LEG_LIMIT = 15
_RRF_K = 60
_MIN_CLUSTER = 3  # fallback-mining threshold: recurring means ≥ this many runs
PROPOSAL_PREFIX = "[proposed from fallback mining] "


def _or_q(task: str) -> str:
    from app.memory.rank import or_tsquery

    return or_tsquery(task)


async def _enabled() -> bool:
    from app.registry_cache import get_cache

    cache = get_cache()
    return bool(await cache.setting("memory_enabled")) and bool(
        await cache.setting("procedural_learning_enabled")
    )


# ── routing stats ─────────────────────────────────────────────────────


async def update_routing_stats(run_id: UUID) -> None:
    """Fold one finished run's route steps into per-capability stats."""
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        if run is None or run.status not in {"completed", "failed"}:
            return
        steps = list(
            (
                await session.execute(
                    select(RunStep).where(RunStep.run_id == run_id, RunStep.step_type == "route")
                )
            ).scalars()
        )
        hitl_denied = bool(
            (
                await session.execute(
                    select(RunStep).where(
                        RunStep.run_id == run_id,
                        RunStep.step_type == "hitl",
                    )
                )
            )
            .scalars()
            .all()
            and any(
                (s.output or {}).get("status") == "denied"
                for s in (
                    await session.execute(
                        select(RunStep).where(RunStep.run_id == run_id, RunStep.step_type == "hitl")
                    )
                ).scalars()
            )
        )
        duration_ms = 0.0
        if run.finished_at and run.started_at:
            duration_ms = (run.finished_at - run.started_at).total_seconds() * 1000
        for step in steps:
            out = step.output or {}
            rung = out.get("rung")
            if not rung:
                continue
            resolved = out.get("resolved_to") or {}
            entity_id = resolved.get("entity_id")
            entity_name = resolved.get("entity_name")
            key = f"{rung}:{entity_id or entity_name or '-'}"[:320]
            stat = await session.get(RoutingStat, key)
            if stat is None:
                stat = RoutingStat(
                    capability_key=key,
                    rung=str(rung),
                    entity_id=UUID(entity_id) if entity_id else None,
                    entity_name=entity_name,
                    runs_total=0,
                    runs_completed=0,
                    runs_failed=0,
                    hitl_denied=0,
                    mean_input_tokens=0.0,
                    mean_output_tokens=0.0,
                    mean_duration_ms=0.0,
                )
                session.add(stat)
            n = stat.runs_total
            stat.runs_total = n + 1
            if run.status == "completed":
                stat.runs_completed += 1
            else:
                stat.runs_failed += 1
            if hitl_denied:
                stat.hitl_denied += 1
            stat.mean_input_tokens = (stat.mean_input_tokens * n + run.total_input_tokens) / (n + 1)
            stat.mean_output_tokens = (stat.mean_output_tokens * n + run.total_output_tokens) / (
                n + 1
            )
            stat.mean_duration_ms = (stat.mean_duration_ms * n + duration_ms) / (n + 1)
            stat.last_used_at = datetime.now(UTC)
        await session.commit()


# ── plan exemplars: harvest + vote lifecycle ──────────────────────────


def _positive_signal(run: Run, steps: list[RunStep]) -> bool:
    if run.status != "completed":
        return False
    denied = any(
        s.step_type == "hitl" and (s.output or {}).get("status") == "denied" for s in steps
    )
    return not denied


async def harvest_exemplar(run_id: UUID) -> PlanExemplar | None:
    """Store a positively-signaled run's routing shape, keyed by task text."""
    from app.memory.store import _embed_ref

    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        if run is None or run.orchestrator_mode not in {"graph", "agentic"}:
            return None
        steps = list(
            (await session.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars()
        )
        if not _positive_signal(run, steps):
            return None
        routes = [
            {
                "rung": (s.output or {}).get("rung"),
                "entity": ((s.output or {}).get("resolved_to") or {}).get("entity_name"),
            }
            for s in steps
            if s.step_type == "route" and (s.output or {}).get("rung")
        ]
        if not routes and not (run.plan or {}).get("entries"):
            return None  # nothing procedural to learn (direct answers etc.)
        existing = (
            await session.execute(select(PlanExemplar).where(PlanExemplar.run_id == run_id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        trace: dict[str, Any] = {"routes": routes}
        if run.orchestrator_mode == "graph" and run.plan:
            trace["plan_entries"] = (run.plan or {}).get("entries", [])
        exemplar = PlanExemplar(
            run_id=run_id,
            task_text=" ".join((run.chat_message or "").split())[:1000],
            mode=run.orchestrator_mode,
            trace=trace,
            votes=1,
            status="active",
        )
        session.add(exemplar)
        await session.flush()
        await _embed_ref(session, exemplar.id, "plan_exemplars", exemplar.task_text)
        await session.commit()
        await session.refresh(exemplar)
    logger.info(
        "memory_exemplar_harvest",
        tier="memory",
        kind="harvest",
        run_id=str(run_id),
        routes=len(routes),
    )
    return exemplar


async def vote_exemplars(exemplar_ids: list[UUID], *, success: bool) -> None:
    """ExpeL lifecycle: reuse outcome adjusts votes; zero retires."""
    if not exemplar_ids:
        return
    async with get_session_factory()() as session:
        delta = 1 if success else -1
        await session.execute(
            update(PlanExemplar)
            .where(PlanExemplar.id.in_(exemplar_ids))
            .values(votes=PlanExemplar.votes + delta)
        )
        await session.execute(
            update(PlanExemplar)
            .where(PlanExemplar.id.in_(exemplar_ids), PlanExemplar.votes <= 0)
            .values(status="retired")
        )
        await session.commit()
    logger.info(
        "memory_exemplar_vote",
        tier="memory",
        kind="harvest",
        count=len(exemplar_ids),
        success=success,
    )


async def recall_exemplars(task: str, *, k: int = 2) -> list[PlanExemplar]:
    """Hybrid recall over active exemplars (same RRF shape as memories)."""
    from app.memory.store import active_model_key
    from app.retrieval import _query_vector

    task = " ".join(task.split())
    if not task:
        return []
    async with get_session_factory()() as session:
        lex_ids = [
            r[0]
            for r in (
                await session.execute(
                    sql_text(
                        """
                        SELECT e.id FROM plan_exemplars e,
                               to_tsquery('english', :q) tsq
                        WHERE e.fts @@ tsq AND e.status = 'active'
                        ORDER BY ts_rank_cd(e.fts, tsq) DESC
                        LIMIT :n
                        """
                    ),
                    {"q": _or_q(task), "n": _EXEMPLAR_LEG_LIMIT},
                )
            ).all()
        ]
        vec_ids: list[UUID] = []
        qvec = await _query_vector(task)
        model_key = await active_model_key() if qvec is not None else None
        if qvec is not None and model_key is not None:
            vec_ids = [
                r[0]
                for r in (
                    await session.execute(
                        sql_text(
                            """
                            SELECT e.id FROM plan_exemplars e
                            JOIN memory_embeddings emb
                              ON emb.ref_id = e.id AND emb.table_ref = 'plan_exemplars'
                             AND emb.model_key = :model_key
                            WHERE e.status = 'active'
                            ORDER BY emb.embedding <=> CAST(:qvec AS vector)
                            LIMIT :n
                            """
                        ),
                        {
                            "q": _or_q(task),
                            "n": _EXEMPLAR_LEG_LIMIT,
                            "qvec": str(list(qvec)),
                            "model_key": model_key,
                        },
                    )
                ).all()
            ]
        rrf: dict[UUID, float] = {}
        for ranking in ([lex_ids] if lex_ids else []) + ([vec_ids] if vec_ids else []):
            for i, eid in enumerate(ranking):
                rrf[eid] = rrf.get(eid, 0.0) + 1.0 / (_RRF_K + i + 1)
        if not rrf:
            return []
        ordered = sorted(rrf, key=lambda eid: rrf[eid], reverse=True)[:k]
        rows = {
            e.id: e
            for e in (
                await session.execute(select(PlanExemplar).where(PlanExemplar.id.in_(ordered)))
            ).scalars()
        }
    return [rows[eid] for eid in ordered if eid in rows]


async def exemplar_block(task: str) -> tuple[str, list[UUID]]:
    """The planner's budgeted few-shot block (spec §16.5), or ("", [])."""
    if not await _enabled():
        return "", []
    try:
        exemplars = await recall_exemplars(task, k=2)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("memory_exemplar_recall_failed", error=str(exc))
        return "", []
    if not exemplars:
        return "", []
    lines = []
    for e in exemplars:
        routes = " → ".join(
            f"{r.get('rung')}({r.get('entity')})" for r in (e.trace or {}).get("routes", [])
        )
        lines.append(f'- past ask: "{e.task_text[:160]}" → what worked: {routes or "direct plan"}')
    block = (
        "\nSimilar past asks and the routing that worked (guidance, not law):\n"
        + "\n".join(lines)
        + "\n"
    )
    ids = [e.id for e in exemplars]
    logger.info(
        "memory_exemplar_inject", tier="memory", kind="inject", surface="planner", count=len(ids)
    )
    return block, ids


async def post_run_procedural(run_id: UUID) -> None:
    """Scheduler chain entry: stats + exemplar harvest + reuse voting."""
    if not await _enabled():
        return
    await update_routing_stats(run_id)
    await harvest_exemplar(run_id)
    # vote on exemplars this run REUSED (recorded on the run context at inject)
    from app.orchestrator.context import get_run_context

    ctx = get_run_context()
    used: list[UUID] = list(getattr(ctx, "used_exemplar_ids", []) or []) if ctx else []
    if used:
        async with get_session_factory()() as session:
            run = await session.get(Run, run_id)
        if run is not None:
            await vote_exemplars(used, success=run.status == "completed")


# ── fallback mining → skill proposals ─────────────────────────────────


async def mine_fallback_skills() -> list[str]:
    """Cluster fallback runs' digests; a recurring cluster drafts an INACTIVE
    dynamic-skill proposal binding exactly the tools those runs actually used
    (spec §16.5). Returns the names of proposals created."""
    if not await _enabled():
        return []
    from app.models import Skill, Tool

    async with get_session_factory()() as session:
        digests = list(
            (
                await session.execute(
                    sql_text(
                        """
                        SELECT d.id, d.run_id, d.text FROM run_digests d
                        WHERE jsonb_exists(d.signals->'rungs', 'fallback')
                        ORDER BY d.created_at DESC LIMIT 200
                        """
                    )
                )
            ).all()
        )
        if len(digests) < _MIN_CLUSTER:
            return []

        # greedy lexical clustering over digest token sets (deterministic)
        def tokens(text: str) -> set[str]:
            return {t for t in text.lower().split() if len(t) > 3}

        clusters: list[list[tuple[Any, Any, str]]] = []
        for row in digests:
            placed = False
            for cluster in clusters:
                seed_tokens = tokens(cluster[0][2])
                overlap = len(tokens(row[2]) & seed_tokens) / max(len(seed_tokens), 1)
                if overlap >= 0.4:
                    cluster.append(tuple(row))
                    placed = True
                    break
            if not placed:
                clusters.append([tuple(row)])
        proposals: list[str] = []
        for cluster in clusters:
            if len(cluster) < _MIN_CLUSTER:
                continue
            run_ids = [c[1] for c in cluster]
            tool_rows = (
                await session.execute(
                    select(RunStep).where(
                        RunStep.run_id.in_(run_ids), RunStep.step_type == "tool_call"
                    )
                )
            ).scalars()
            entity_ids = {s.sub_agent_id for s in tool_rows if s.sub_agent_id}
            # tool entity ids are recorded per step via obs labels; resolve the
            # tools these runs touched by name from steps' node ids is brittle —
            # bind by tool usage recorded in signals when available, else skip
            tool_keys: set[str] = set()
            for c in cluster:
                step_tools = (
                    await session.execute(
                        sql_text(
                            """
                            SELECT DISTINCT rs.node_id FROM run_steps rs
                            WHERE rs.run_id = :rid AND rs.step_type = 'tool_call'
                            """
                        ),
                        {"rid": c[1]},
                    )
                ).all()
                tool_keys.update(t[0] for t in step_tools if t[0])
            del entity_ids
            bound = list(
                (await session.execute(select(Tool).where(Tool.tool_key.in_(tool_keys)))).scalars()
            )
            if not bound:
                continue
            name = f"mined-{abs(hash(cluster[0][2])) % 10_000:04d}"
            exists = (
                await session.execute(select(Skill).where(Skill.name == name))
            ).scalar_one_or_none()
            if exists is not None:
                continue
            summary = cluster[0][2][:160]
            skill = Skill(
                name=name,
                description=PROPOSAL_PREFIX + f"covers a recurring uncovered ask: {summary}",
                persona="You handle a recurring request the registry did not cover. "
                "Follow the task precisely and use only your bound tools.",
                instructions=f"Recurring ask cluster ({len(cluster)} fallback runs). "
                f"Representative: {summary}",
                kind="custom",
                source="dynamic",
                status="inactive",  # human review activates (spec §16.5)
                direct_exposure=False,
                tools=bound,
            )
            session.add(skill)
            proposals.append(name)
        if proposals:
            await session.commit()
    if proposals:
        from app.registry_cache import get_cache

        await get_cache().invalidate("skills")
        logger.info("memory_skill_proposals", tier="memory", kind="harvest", proposals=proposals)
    return proposals
