"""Memory retrieval (spec §16.3).

Hybrid candidates in SQL (lexical GIN + pgvector cosine over the active
model_key), composite scoring in Python over the fused candidate set:

    score = (w_rel·relevance + w_rec·recency + w_imp·importance/10) / Σw

- relevance: normalized RRF of the two rank lists (rank positions, so the
  lexical leg's lack of IDF is blunted — research 04 §2)
- recency:   exp(−ln2 · hours_since_last_ACCESS / half_life) — rehearsal
  refreshes memories (Generative Agents; research 03 §2)
- importance: the write-time 1–10 score

Score floor: below it nothing is returned — an empty block beats a
distracting one (research 03 §6). Pinned rows bypass the floor (they are the
always-injected profile, spec §16.3). `as_of` switches to bi-temporal
point-in-time predicates. No embedding model ⇒ lexical-only, silently.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import bindparam, select, update
from sqlalchemy import text as sql_text

from app.db import get_session_factory
from app.models import Memory

logger = structlog.get_logger("memory")

_RRF_K = 60
_CANDIDATES_PER_LEG = 40
W_REL, W_REC, W_IMP = 1.0, 0.8, 0.6
DEFAULT_HALF_LIFE_HOURS = 30.0 * 24


def _auth_uid() -> Any:
    from app.auth import current_user_id

    return current_user_id()


@dataclass
class RecallHit:
    memory: Memory
    score: float
    relevance: float
    recency: float
    importance: float
    linked: bool = False  # §16.7: reached via an entity hop, not similarity


def _temporal_predicate(as_of: datetime | None) -> str:
    if as_of is None:
        return "m.status = 'active'"
    # what was believed at T about facts valid at T (research 04 §4);
    # rows that never activated (quarantined/rejected) are excluded
    return (
        "m.status NOT IN ('quarantined', 'rejected') "
        "AND m.recorded_at <= :as_of "
        "AND (m.superseded_at IS NULL OR m.superseded_at > :as_of) "
        "AND m.valid_from <= :as_of "
        "AND (m.valid_to IS NULL OR m.valid_to > :as_of)"
    )


def _filters_sql(scopes: list[str] | None, kinds: list[str] | None) -> str:
    parts = []
    if scopes:
        parts.append("m.scope = ANY(:scopes)")
    if kinds:
        parts.append("m.kind = ANY(:kinds)")
    parts.append("(m.scope != 'conversation' OR m.conversation_id = :conversation_id)")
    # §18.2: project rows are visible only under their own key; without a
    # key the equality is NULL ⇒ false — projects never leak sideways
    parts.append("(m.scope != 'project' OR m.project_key = CAST(:project_key AS text))")
    # §18.8 tenancy: another user's memories are invisible; unowned
    # (pre-auth / system) rows stay visible to everyone
    from app.auth import auth_enabled

    if auth_enabled():
        parts.append("(m.user_id = CAST(:auth_user_id AS uuid) OR m.user_id IS NULL)")
    return (" AND " + " AND ".join(parts)) if parts else ""


async def recall(
    query: str,
    *,
    scopes: list[str] | None = None,
    kinds: list[str] | None = None,
    conversation_id: UUID | None = None,
    project_key: str | None = None,
    k: int = 6,
    floor: float = 0.35,
    as_of: datetime | None = None,
    bump_access: bool = True,
) -> list[RecallHit]:
    """Top-k memories for a query under the composite score, floor-gated."""
    from app.memory.store import active_model_key
    from app.retrieval import _query_vector

    query = " ".join(query.split())
    if not query:
        return []

    params: dict[str, Any] = {
        "q": or_tsquery(query),
        "n": _CANDIDATES_PER_LEG,
        "scopes": scopes,
        "kinds": kinds,
        "conversation_id": conversation_id,
        "project_key": project_key,
        "as_of": as_of,
        "auth_user_id": str(_auth_uid()) if _auth_uid() else None,
    }
    where = _temporal_predicate(as_of) + _filters_sql(scopes, kinds)

    lexical_sql = sql_text(
        f"""
        SELECT m.id FROM memories m, to_tsquery('english', :q) tsq
        WHERE m.fts @@ tsq AND {where}
        ORDER BY ts_rank_cd(m.fts, tsq) DESC
        LIMIT :n
        """
    )
    qvec = await _query_vector(query)
    model_key = await active_model_key() if qvec is not None else None

    async with get_session_factory()() as session:
        lex_ids = [r[0] for r in (await session.execute(lexical_sql, params)).all()]
        vec_ids: list[UUID] = []
        if qvec is not None and model_key is not None:
            vector_sql = sql_text(
                f"""
                SELECT m.id, 1 - (e.embedding <=> CAST(:qvec AS vector)) AS sim
                FROM memories m
                JOIN memory_embeddings e
                  ON e.ref_id = m.id AND e.table_ref = 'memories'
                 AND e.model_key = :model_key
                WHERE {where}
                ORDER BY e.embedding <=> CAST(:qvec AS vector)
                LIMIT :n
                """
            ).bindparams(bindparam("qvec"), bindparam("model_key"))
            vec_rows = (
                await session.execute(
                    vector_sql,
                    {**params, "qvec": str(list(qvec)), "model_key": model_key},
                )
            ).all()
            vec_ids = [r[0] for r in vec_rows]

        # reciprocal-rank fusion over whichever legs exist
        rrf: dict[UUID, float] = {}
        for ranking in ([lex_ids] if lex_ids else []) + ([vec_ids] if vec_ids else []):
            for i, mid in enumerate(ranking):
                rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (_RRF_K + i + 1)
        if not rrf:
            return []
        max_rrf = max(rrf.values())

        # ORM load by id — NEVER a raw `SELECT *` with positional column
        # mapping: a migrated DB's column order can differ from the model's
        # (found live in M31: project_key appended last shifted every later
        # column onto the wrong result processor)
        mem_rows = list(
            (await session.execute(select(Memory).where(Memory.id.in_(list(rrf.keys())))))
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        hits: list[RecallHit] = []
        for mem in mem_rows:
            relevance = rrf[mem.id] / max_rrf
            half_life_h = (
                float(mem.half_life_days) * 24 if mem.half_life_days else DEFAULT_HALF_LIFE_HOURS
            )
            anchor = mem.last_accessed_at or mem.recorded_at
            age_h = max((now - anchor).total_seconds() / 3600.0, 0.0)
            recency = math.exp(-math.log(2) / half_life_h * age_h)
            importance = mem.importance / 10.0
            score = (W_REL * relevance + W_REC * recency + W_IMP * importance) / (
                W_REL + W_REC + W_IMP
            )
            if mem.pinned or score >= floor:
                hits.append(
                    RecallHit(
                        memory=mem,
                        score=round(score, 4),
                        relevance=round(relevance, 4),
                        recency=round(recency, 4),
                        importance=importance,
                    )
                )
        hits.sort(key=lambda h: h.score, reverse=True)
        hits = hits[:k]

        # §16.7 entity hop: up to 2 extra active memories sharing an entity
        # with a top hit — reached by structure, not similarity, so they are
        # floor-exempt, scored at a fixed discount of the weakest direct hit,
        # and skipped for filtered (kinds) or point-in-time (as_of) recalls
        if hits and as_of is None and kinds is None:
            hop_rows = (
                await session.execute(
                    sql_text(
                        """
                        SELECT DISTINCT m.id FROM memories m
                        JOIN memory_entity_links l1 ON l1.memory_id = m.id
                        JOIN memory_entity_links l2 ON l2.entity_id = l1.entity_id
                        WHERE l2.memory_id = ANY(:hit_ids)
                          AND m.id != ALL(:hit_ids)
                          AND m.status = 'active'
                          AND (m.scope != 'conversation'
                               OR m.conversation_id = :conversation_id)
                        LIMIT 2
                        """
                    ),
                    {
                        "hit_ids": [h.memory.id for h in hits],
                        "conversation_id": conversation_id,
                    },
                )
            ).all()
            hop_ids = [r[0] for r in hop_rows]
            if hop_ids:
                hop_score = round(min(h.score for h in hits) * 0.8, 4)
                hop_mems = list(
                    (await session.execute(select(Memory).where(Memory.id.in_(hop_ids))))
                    .scalars()
                    .all()
                )
                for mem in hop_mems:
                    hits.append(
                        RecallHit(
                            memory=mem,
                            score=hop_score,
                            relevance=0.0,
                            recency=0.0,
                            importance=mem.importance / 10.0,
                            linked=True,
                        )
                    )

        if bump_access and hits and as_of is None:
            await session.execute(
                update(Memory)
                .where(Memory.id.in_([h.memory.id for h in hits]))
                .values(last_accessed_at=now, access_count=Memory.access_count + 1)
            )
            await session.commit()

    logger.info(
        "memory_recall",
        tier="memory",
        kind="recall",
        query_len=len(query),
        candidates=len(rrf),
        returned=len(hits),
        vector_leg=bool(vec_ids),
    )
    return hits


async def pinned_memories(conversation_id: UUID | None = None) -> list[Memory]:
    """The always-injected profile rows (spec §16.3), newest first."""
    from sqlalchemy import select

    async with get_session_factory()() as session:
        stmt = (
            select(Memory)
            .where(Memory.pinned.is_(True), Memory.status == "active")
            .order_by(Memory.recorded_at.desc())
        )
        rows = list((await session.execute(stmt)).scalars())
    return [m for m in rows if m.scope != "conversation" or m.conversation_id == conversation_id]


def or_tsquery(query: str) -> str:
    """OR-joined tsquery source: recall ranks by ts_rank_cd over ANY matching
    term — websearch_to_tsquery's AND semantics let question boilerplate
    ("one short sentence") veto the real match (experiment finding, M17)."""
    import re as _re

    tokens = _re.findall(r"[a-z0-9]+", query.lower())
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
    return " | ".join(seen[:24]) or "x_no_terms"
