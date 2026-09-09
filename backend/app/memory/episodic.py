"""L1 episodic layer (spec §16.2) — run digests and conversation rollups.

Digests are the round-level retrieval unit (fact/round-level indexing beats
summary-level — research 03 §6); rollups serve sense-making and the UI, never
the primary index. The digest text prefers one cheap LLM call (extraction
model, else default at effort low) and falls back to a mechanical digest —
a digest is never allowed to fail the pipeline.
"""

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import bindparam, delete, select
from sqlalchemy import text as sql_text

from app.db import get_session_factory
from app.models import ConversationRollup, MemoryEmbedding, Run, RunDigest, RunStep

logger = structlog.get_logger("memory")

_RRF_K = 60
_LEG_LIMIT = 25
_DIGEST_HALF_LIFE_HOURS = 14 * 24.0


async def harvest_signals(run: Run, steps: list[RunStep]) -> dict[str, Any]:
    """Mechanical outcome signals — no model judgment involved."""
    rungs = [
        (s.output or {}).get("rung")
        for s in steps
        if s.step_type == "route" and (s.output or {}).get("rung")
    ]
    hitl = [
        {
            "status": (s.output or {}).get("status"),
            "note": (s.output or {}).get("note"),
        }
        for s in steps
        if s.step_type == "hitl" and s.output
    ]
    duration_ms = None
    if run.finished_at and run.started_at:
        duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
    return {
        "status": run.status,
        "mode": run.orchestrator_mode,
        "rungs": rungs,
        "hitl": hitl,
        "denied": any(h.get("status") == "denied" for h in hitl),
        "input_tokens": run.total_input_tokens,
        "output_tokens": run.total_output_tokens,
        "duration_ms": duration_ms,
    }


def _mechanical_digest(run: Run) -> str:
    ask = " ".join((run.chat_message or "").split())[:220]
    answer = " ".join((run.final_answer or "").split())[:220]
    text = f"Asked: {ask} — outcome: {run.status}."
    if answer:
        text += f" Answer: {answer}"
    return text


async def _llm_digest(run: Run) -> str | None:
    """One cheap call; any failure returns None (mechanical fallback)."""
    try:
        from app.llm import ModelParams, get_model, text_from_content
        from app.prompts import load_prompt
        from app.registry_cache import get_cache

        cache = get_cache()
        ref = await cache.setting("memory_extraction_model") or await cache.setting("default_model")
        raw_params = await cache.setting("memory_extraction_model_params")
        params = ModelParams.model_validate(raw_params) if raw_params else ModelParams(effort="low")
        model = get_model(str(ref), params)
        prompt = load_prompt("memory_digest").format(
            task=(run.chat_message or "")[:1500],
            answer=(run.final_answer or "")[:1500],
            status=run.status,
        )
        ai = await model.ainvoke(prompt)
        text = " ".join(text_from_content(ai.content).split())
        return text[:600] or None
    except Exception as exc:  # noqa: BLE001 — digests never fail the pipeline
        logger.info("memory_digest_llm_fallback", error=str(exc))
        return None


async def digest_run(run_id: UUID) -> RunDigest | None:
    """Create (or return the existing) digest for a completed run."""
    from app.memory.store import _embed_ref

    async with get_session_factory()() as session:
        existing = (
            await session.execute(select(RunDigest).where(RunDigest.run_id == run_id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        run = await session.get(Run, run_id)
        if run is None or run.status not in {"completed", "failed", "cancelled"}:
            return None
        steps = list(
            (await session.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars()
        )
        signals = await harvest_signals(run, steps)
        text = await _llm_digest(run) or _mechanical_digest(run)
        digest = RunDigest(
            run_id=run_id,
            conversation_id=run.conversation_id,
            text=text,
            signals=signals,
        )
        session.add(digest)
        await session.flush()
        await _embed_ref(session, digest.id, "run_digests", digest.text)
        await session.commit()
        await session.refresh(digest)
    logger.info(
        "memory_digest",
        tier="memory",
        kind="digest",
        run_id=str(run_id),
        chars=len(text),
    )
    return digest


async def update_rollup(conversation_id: UUID) -> ConversationRollup:
    """Refresh the conversation rollup from its digests (mechanical summary of
    summaries — rollups are a browsing/sense-making surface, spec §16.2)."""
    async with get_session_factory()() as session:
        digests = list(
            (
                await session.execute(
                    select(RunDigest)
                    .where(RunDigest.conversation_id == conversation_id)
                    .order_by(RunDigest.created_at)
                )
            ).scalars()
        )
        text = " ".join(d.text for d in digests[-12:])[:2400] or "(no completed runs yet)"
        rollup = await session.get(ConversationRollup, conversation_id)
        if rollup is None:
            rollup = ConversationRollup(
                conversation_id=conversation_id, text=text, runs_covered=len(digests)
            )
            session.add(rollup)
        else:
            rollup.text = text
            rollup.runs_covered = len(digests)
        await session.commit()
        await session.refresh(rollup)
    logger.info(
        "memory_rollup",
        tier="memory",
        kind="rollup",
        conversation_id=str(conversation_id),
        runs_covered=rollup.runs_covered,
    )
    return rollup


async def compact_digests(now: datetime | None = None) -> int:
    """§16.7 digest compaction: fold run-digests older than
    `memory_digest_compact_days` into one `period` digest per conversation
    (merging into the existing period row when one exists), then hard-delete
    the folded rows and their embeddings. Keeps the episodic store
    O(conversations), not O(runs). Returns the number of digests folded.
    Gated in-function (M48 §3.7.1) — this is the one consolidation job
    with an irreversible effect, so no call path may reach the delete
    while the switch is off."""
    from app.memory.lifecycle import JOB_COMPACT, JOB_GATES, gate_open
    from app.memory.store import _embed_ref
    from app.registry_cache import get_cache

    if not await gate_open(JOB_GATES[JOB_COMPACT]):
        return 0
    days = int(await get_cache().setting("memory_digest_compact_days"))
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    folded = 0
    async with get_session_factory()() as session:
        old = list(
            (
                await session.execute(
                    select(RunDigest)
                    .where(RunDigest.kind == "run", RunDigest.created_at < cutoff)
                    .order_by(RunDigest.conversation_id, RunDigest.created_at)
                )
            ).scalars()
        )
        by_conv: dict[UUID, list[RunDigest]] = {}
        for d in old:
            by_conv.setdefault(d.conversation_id, []).append(d)
        for conv_id, digests in by_conv.items():
            period = (
                await session.execute(
                    select(RunDigest).where(
                        RunDigest.conversation_id == conv_id, RunDigest.kind == "period"
                    )
                )
            ).scalar_one_or_none()
            pieces = ([period.text] if period is not None else []) + [d.text for d in digests]
            text = " ".join(" ".join(p.split()) for p in pieces)[:2400]
            prev = int((period.signals or {}).get("runs_folded", 0)) if period is not None else 0
            runs_folded = len(digests) + prev
            covers_from = min(
                [d.created_at for d in digests]
                + ([period.covers_from] if period is not None and period.covers_from else [])
            )
            covers_to = max(
                [d.created_at for d in digests]
                + ([period.covers_to] if period is not None and period.covers_to else [])
            )
            if period is None:
                period = RunDigest(
                    run_id=None,
                    conversation_id=conv_id,
                    kind="period",
                    text=text,
                    signals={"runs_folded": runs_folded},
                    covers_from=covers_from,
                    covers_to=covers_to,
                )
                session.add(period)
                await session.flush()
            else:
                period.text = text
                period.signals = {"runs_folded": runs_folded}
                period.covers_from = covers_from
                period.covers_to = covers_to
            digest_ids = [d.id for d in digests]
            await session.execute(
                delete(MemoryEmbedding).where(
                    MemoryEmbedding.table_ref == "run_digests",
                    MemoryEmbedding.ref_id.in_(digest_ids),
                )
            )
            await session.execute(delete(RunDigest).where(RunDigest.id.in_(digest_ids)))
            await _embed_ref(session, period.id, "run_digests", period.text)
            folded += len(digests)
        if folded:
            await session.commit()
    from app import obs

    obs.MEMORY_OPS.labels(kind="compact", status="ok").inc()
    logger.info("memory_compact", tier="memory", kind="compact", folded=folded)
    return folded


async def recall_digests(
    query: str,
    *,
    k: int = 3,
    exclude_conversation_id: UUID | None = None,
) -> list[tuple[RunDigest, float]]:
    """Hybrid recall over run digests (round-level episodic retrieval).
    Same RRF shape as memory recall; recency-weighted, no importance term."""
    from app.memory.store import active_model_key
    from app.retrieval import _query_vector

    query = " ".join(query.split())
    if not query:
        return []
    from app.memory.rank import or_tsquery

    params: dict[str, Any] = {
        "q": or_tsquery(query),
        "n": _LEG_LIMIT,
        "excl": exclude_conversation_id,
    }
    excl = "AND (CAST(:excl AS uuid) IS NULL OR d.conversation_id != CAST(:excl AS uuid))"

    async with get_session_factory()() as session:
        lex_rows = (
            await session.execute(
                sql_text(
                    f"""
                    SELECT d.id FROM run_digests d,
                           to_tsquery('english', :q) tsq
                    WHERE d.fts @@ tsq {excl}
                    ORDER BY ts_rank_cd(d.fts, tsq) DESC
                    LIMIT :n
                    """
                ),
                params,
            )
        ).all()
        lex_ids = [r[0] for r in lex_rows]
        vec_ids: list[UUID] = []
        qvec = await _query_vector(query)
        model_key = await active_model_key() if qvec is not None else None
        if qvec is not None and model_key is not None:
            vec_rows = (
                await session.execute(
                    sql_text(
                        f"""
                        SELECT d.id FROM run_digests d
                        JOIN memory_embeddings e
                          ON e.ref_id = d.id AND e.table_ref = 'run_digests'
                         AND e.model_key = :model_key
                        WHERE true {excl}
                        ORDER BY e.embedding <=> CAST(:qvec AS vector)
                        LIMIT :n
                        """
                    ).bindparams(bindparam("qvec"), bindparam("model_key")),
                    {**params, "qvec": str(list(qvec)), "model_key": model_key},
                )
            ).all()
            vec_ids = [r[0] for r in vec_rows]

        rrf: dict[UUID, float] = {}
        for ranking in ([lex_ids] if lex_ids else []) + ([vec_ids] if vec_ids else []):
            for i, did in enumerate(ranking):
                rrf[did] = rrf.get(did, 0.0) + 1.0 / (_RRF_K + i + 1)
        if not rrf:
            return []
        max_rrf = max(rrf.values())
        rows = list(
            (await session.execute(select(RunDigest).where(RunDigest.id.in_(rrf.keys())))).scalars()
        )
    now = datetime.now(UTC)
    scored: list[tuple[RunDigest, float]] = []
    for d in rows:
        age_h = max((now - d.created_at).total_seconds() / 3600.0, 0.0)
        recency = math.exp(-math.log(2) / _DIGEST_HALF_LIFE_HOURS * age_h)
        score = (1.0 * (rrf[d.id] / max_rrf) + 0.6 * recency) / 1.6
        scored.append((d, round(score, 4)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]
