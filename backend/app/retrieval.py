"""Progressive-disclosure retrieval (spec §7.4).

Ranks registry catalogs down to the top-K records most relevant to the
current task before they are injected into a model call. Scoring runs
in-process over the cache snapshot — never a per-call DB query:

- lexical: BM25 (Okapi) over name + description tokens
- vector: cosine over stored embeddings (§2.1 embeddings port), when both
  the record vector and a query vector are available
- fusion: reciprocal-rank fusion of whichever score lists exist

Guarantees (spec §7.4): activates only above ``retrieval_threshold`` records;
pinned ids (entities already used in the run) bypass ranking; full-catalog
mode never ranks; every truncation logs its drop count and the caller can
render a catalog footer so the model knows it sees a slice.
"""

import hashlib
import math
import re
from typing import Any

import structlog

logger = structlog.get_logger("retrieval")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RRF_K = 60
# memoized query embeddings: (model, text-hash) → vector, small LRU
_QUERY_VECS: dict[tuple[str, str], list[float]] = {}
_QUERY_VECS_MAX = 128


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def embed_text_for(record: dict[str, Any], kind: str) -> str:
    """The text a record is embedded under. Retrieval may index more than
    disclosure shows (spec §7.4): summaries go to the model, full definitions
    may inform the index."""
    parts = [str(record.get("name", "")), str(record.get("description") or "")]
    if kind == "skills":
        parts.append(str(record.get("persona") or ""))
        parts.append(str(record.get("instructions") or "")[:1000])
        parts.extend(t.get("tool_key", "") for t in record.get("tools", []))
    elif kind == "sub_agents":
        parts.append(str(record.get("persona") or "")[:500])
        parts.extend(record.get("skill_names", []))
    elif kind == "tools":
        parts.append(str(record.get("tool_key") or ""))
    return "\n".join(p for p in parts if p)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── scoring ──────────────────────────────────────────────────────


def bm25_scores(query: str, docs: list[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi BM25 of the query against each doc (no external deps —
    catalogs are hundreds of records, not millions)."""
    q_tokens = tokenize(query)
    doc_tokens = [tokenize(d) for d in docs]
    n = len(docs)
    if n == 0 or not q_tokens:
        return [0.0] * n
    avg_len = sum(len(t) for t in doc_tokens) / n or 1.0
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    scores = [0.0] * n
    for term in q_tokens:
        d = df.get(term)
        if not d:
            continue
        idf = math.log(1 + (n - d + 0.5) / (d + 0.5))
        for i, tokens in enumerate(doc_tokens):
            tf = tokens.count(term)
            if tf:
                scores[i] += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(tokens) / avg_len))
    return scores


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rrf_fuse(rankings: list[list[int]], size: int) -> list[int]:
    """Reciprocal-rank fusion: each ranking is a list of indices, best first.
    Returns all indices ordered by fused score."""
    scores = [0.0] * size
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] += 1.0 / (_RRF_K + rank + 1)
    return sorted(range(size), key=lambda i: (-scores[i], i))


def rank_records(
    query: str,
    records: list[dict[str, Any]],
    *,
    query_vec: list[float] | None = None,
) -> list[int]:
    """Fused relevance order (indices into records, best first)."""
    docs = [f"{r.get('name', '')} {r.get('description') or ''}" for r in records]
    rankings: list[list[int]] = []
    lex = bm25_scores(query, docs)
    if any(lex):
        rankings.append(sorted(range(len(records)), key=lambda i: (-lex[i], i)))
    if query_vec is not None:
        vec_scores = [
            cosine(query_vec, r.get("embedding") or []) if r.get("embedding") else 0.0
            for r in records
        ]
        if any(vec_scores):
            rankings.append(sorted(range(len(records)), key=lambda i: (-vec_scores[i], i)))
    if not rankings:
        return list(range(len(records)))
    return rrf_fuse(rankings, len(records))


# ── the catalog gate (consumed by middlewares + planner) ─────────


async def _query_vector(query: str) -> list[float] | None:
    from app.registry_cache import get_cache

    model = await get_cache().setting("embedding_model")
    if not model:
        return None
    key = (str(model), text_hash(query))
    if key in _QUERY_VECS:
        return _QUERY_VECS[key]
    try:
        from app.llm import get_embeddings

        vec = (await get_embeddings(str(model), [query]))[0]
    except Exception as exc:  # noqa: BLE001 — degrade to lexical-only
        logger.warning("query_embedding_failed", error=str(exc))
        return None
    if len(_QUERY_VECS) >= _QUERY_VECS_MAX:
        _QUERY_VECS.pop(next(iter(_QUERY_VECS)))
    _QUERY_VECS[key] = vec
    return vec


def apply_ambient_allowlist(records: list[dict[str, Any]], *, kind: str) -> list[dict[str, Any]]:
    """Narrowed registry projection for ambient runs (spec §17.4): a routine
    with an allowlist sees only the named entries of each mentioned kind —
    by id, name, or tool_key. Applied BEFORE ranking and regardless of the
    retrieval settings; a kind the allowlist does not mention is unchanged.
    Identity for every non-ambient run."""
    from app.orchestrator.context import get_run_context

    ctx = get_run_context()
    if ctx is None or ctx.ambient_allowlist is None:
        return records
    allowed = ctx.ambient_allowlist.get(kind)
    if allowed is None:
        return records
    allowed_set = {str(a) for a in allowed}
    kept = [
        r
        for r in records
        if {str(r.get("id")), str(r.get("name")), str(r.get("tool_key"))} & allowed_set
    ]
    if len(kept) != len(records):
        logger.info(
            "ambient_allowlist_narrowed",
            kind=kind,
            total=len(records),
            shown=len(kept),
        )
    return kept


async def apply_retrieval(
    records: list[dict[str, Any]],
    *,
    kind: str,
    query: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Gate a catalog: returns (records-to-inject, dropped_count).

    Below the threshold (or disabled, or no query) this is the identity —
    full injection exactly as before retrieval existed.
    """
    from app.orchestrator.context import get_run_context
    from app.registry_cache import get_cache

    cache = get_cache()
    if not await cache.setting("retrieval_enabled"):
        return records, 0
    threshold = int(await cache.setting("retrieval_threshold"))
    if len(records) <= threshold:
        return records, 0
    ctx = get_run_context()
    if query is None and ctx is not None:
        query = ctx.query_text
    if not query:
        return records, 0
    top_k = int(await cache.setting("retrieval_top_k"))
    pinned: set[str] = set(ctx.pinned_ids) if ctx is not None else set()

    order = rank_records(query, records, query_vec=await _query_vector(query))
    selected: list[int] = [i for i in order[:top_k]]
    # pins bypass ranking — entities already used in this run stay visible
    selected_set = set(selected)
    for i, record in enumerate(records):
        if record.get("id") in pinned and i not in selected_set:
            selected.append(i)
            selected_set.add(i)
    selected.sort()
    dropped = len(records) - len(selected)
    if dropped > 0:
        logger.info(
            "retrieval_truncated_catalog",
            kind=kind,
            total=len(records),
            shown=len(selected),
            dropped=dropped,
        )
    return [records[i] for i in selected], dropped


def catalog_footer(kind: str, shown: int, total: int) -> str:
    """The honesty line (spec §7.4): the model must know it sees a slice."""
    return (
        f"(showing {shown} of {total} {kind} most relevant to the current task — "
        "call use_full_catalog to widen if something you need seems missing)"
    )


# ── write-path embedding maintenance ─────────────────────────────

_EMBED_TASKS: set[Any] = set()


def schedule_embedding(kind: str, record_id: str) -> None:
    """Fire-and-forget embed of a just-written record (spec §7.4: the write
    path never blocks on, or fails because of, embedding)."""
    import asyncio

    task = asyncio.create_task(refresh_record_embedding(kind, record_id))
    _EMBED_TASKS.add(task)
    task.add_done_callback(_EMBED_TASKS.discard)


async def refresh_record_embedding(kind: str, record_id: str) -> bool:
    """Best-effort embed of one registry record (spec §7.4): never raises,
    returns True when the stored vector changed."""
    try:
        from app.registry_cache import get_cache

        cache = get_cache()
        model = await cache.setting("embedding_model")
        if not model:
            return False
        from uuid import UUID

        from app.db import get_session_factory
        from app.models import Skill, SubAgent, Tool
        from app.registry_cache import _skill_record, _sub_agent_record, _tool_record

        orm: dict[str, Any] = {"tools": Tool, "skills": Skill, "sub_agents": SubAgent}
        async with get_session_factory()() as session:
            row = await session.get(orm[kind], UUID(record_id))
            if row is None or row.deleted_at is not None:
                return False
            to_record: dict[str, Any] = {
                "tools": _tool_record,
                "skills": _skill_record,
                "sub_agents": _sub_agent_record,
            }
            record = to_record[kind](row)
            text = embed_text_for(record, kind)
            digest = text_hash(f"{model}:{text}")
            if row.embedding_hash == digest and row.embedding is not None:
                return False
            from app.llm import get_embeddings

            vec = (await get_embeddings(str(model), [text]))[0]
            row.embedding = vec
            row.embedding_hash = digest
            await session.commit()
        from typing import cast

        from app.registry_cache import Registry

        await cache.invalidate(cast(Registry, kind))
        return True
    except Exception as exc:  # noqa: BLE001 — embedding never fails a save
        logger.warning("embedding_refresh_failed", kind=kind, record_id=record_id, error=str(exc))
        return False


async def backfill_embeddings() -> dict[str, int]:
    """Startup/on-enable backfill: embed every record whose hash is stale.
    Returns {registry: updated_count}; safe to run when disabled (no-op)."""
    from app.registry_cache import get_cache

    cache = get_cache()
    if not await cache.setting("retrieval_enabled") or not await cache.setting("embedding_model"):
        return {}
    out: dict[str, int] = {}
    kinds: tuple[Any, ...] = ("tools", "skills", "sub_agents")
    for kind in kinds:
        rows = await cache._ensure(kind)
        assert isinstance(rows, list)
        updated = 0
        for record in rows:
            if await refresh_record_embedding(kind, record["id"]):
                updated += 1
        if updated:
            out[kind] = updated
    if out:
        logger.info("embedding_backfill_done", **out)
    return out
