"""L2 semantic store — writes (spec §16.1/16.2).

Rules enforced here, not in prompts:
- provenance is mandatory on machine writes (extracted/inferred/hitl_note ⇒ run_id)
- extracted/inferred instruction-kind memories always land quarantined; the
  memory.remember TOOL quarantines instruction-kind too (model-mediated text is
  an injection surface — spec §16.4); only the UI/API with source='user_stated'
  activates an instruction directly
- pipelines never hard-delete: supersede/expire only; hard delete is a
  user/purge action
- supersession is append-only and guarded (`WHERE superseded_at IS NULL`)
- the admission gate is deterministic code (confidence floor, length bounds,
  near-duplicate drop) — write policy is the security boundary (research 03 §5)
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_factory
from app.models import Memory, MemoryEmbedding

logger = structlog.get_logger("memory")

KINDS = {"fact", "preference", "entity", "relation", "instruction"}
SCOPES = {"global", "conversation", "project"}
SOURCES = {"extracted", "user_stated", "user_edited", "hitl_note", "inferred"}
MACHINE_SOURCES = {"extracted", "inferred", "hitl_note"}

# admission-gate constants (spec §16.2; deliberate constants, not settings —
# the gate must not be loosened by a runtime toggle)
GATE_MIN_CONFIDENCE = 0.5
GATE_MIN_CHARS = 8
GATE_MAX_CHARS = 600
GATE_DUP_COSINE = 0.95


class MemoryWriteError(ValueError):
    """A write violates a §16 rule (bad kind/scope/source, missing provenance,
    double supersede…)."""


@dataclass
class Candidate:
    """An extraction candidate entering the admission gate."""

    text: str
    kind: str
    scope: str = "global"
    conversation_id: UUID | None = None
    payload: dict[str, Any] | None = None
    entity_key: str | None = None
    importance: int = 5
    confidence: float = 0.7
    valid_from: datetime | None = None
    entities: list[str] = field(default_factory=list)


def _validate(kind: str, scope: str, source: str) -> None:
    errors = []
    if kind not in KINDS:
        errors.append(f"kind must be one of {sorted(KINDS)}")
    if scope not in SCOPES:
        errors.append(f"scope must be one of {sorted(SCOPES)}")
    if source not in SOURCES:
        errors.append(f"source must be one of {sorted(SOURCES)}")
    if errors:
        raise MemoryWriteError("; ".join(errors))


async def active_model_key() -> str | None:
    """The embedding side-table key: 'provider:model@dims' (spec §16.1)."""
    from app.registry_cache import get_cache

    model = await get_cache().setting("embedding_model")
    if not model:
        return None
    try:
        from app.llm import get_embeddings

        probe = await get_embeddings(str(model), ["dims probe"])
        return f"{model}@{len(probe[0])}"
    except Exception as exc:  # noqa: BLE001 — degrade to lexical-only
        logger.warning("memory_model_key_failed", error=str(exc))
        return None


async def _embed_ref(session: AsyncSession, ref_id: UUID, table_ref: str, text: str) -> None:
    """Best-effort write-through embedding (never fails the save)."""
    try:
        from app.llm import get_embeddings
        from app.registry_cache import get_cache

        model = await get_cache().setting("embedding_model")
        if not model:
            return
        vec = (await get_embeddings(str(model), [text]))[0]
        key = f"{model}@{len(vec)}"
        existing = await session.get(MemoryEmbedding, (ref_id, table_ref, key))
        if existing is None:
            session.add(
                MemoryEmbedding(ref_id=ref_id, table_ref=table_ref, model_key=key, embedding=vec)
            )
        else:
            existing.embedding = vec
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_embed_failed", ref=str(ref_id), error=str(exc))


async def _link_entities(sess: AsyncSession, memory_id: UUID, names: list[str]) -> None:
    """§16.7 entity-hop substrate: get-or-create entities by case-insensitive
    name and link them to the memory. Best-effort — never fails the write."""
    try:
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        from app.models import MemoryEntity, MemoryEntityLink

        for raw in names[:3]:
            name = " ".join(raw.split())
            if len(name) < 2:
                continue
            entity = (
                await sess.execute(
                    sa_select(MemoryEntity).where(sa_func.lower(MemoryEntity.name) == name.lower())
                )
            ).scalar_one_or_none()
            if entity is None:
                entity = MemoryEntity(name=name)
                sess.add(entity)
                await sess.flush()
            sess.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity.id))
        await sess.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_entity_link_failed", memory_id=str(memory_id), error=str(exc))


async def remember(
    *,
    text: str,
    kind: str,
    scope: str = "global",
    source: str,
    conversation_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    entity_key: str | None = None,
    importance: int = 5,
    confidence: float = 0.7,
    valid_from: datetime | None = None,
    run_id: UUID | None = None,
    step_id: UUID | None = None,
    supersedes: UUID | None = None,
    via_tool: bool = False,
    entities: list[str] | None = None,
    session: AsyncSession | None = None,
    project_key: str | None = None,
    user_id: UUID | None = None,
) -> Memory:
    """Insert one memory row under the §16.2 rules. Raises MemoryWriteError."""
    _validate(kind, scope, source)
    if user_id is None:
        from app.auth import current_user_id

        user_id = current_user_id()  # §18.8: memories belong to the requester
    if scope == "project" and not project_key:
        raise MemoryWriteError("scope 'project' requires project_key")
    # inferred memories may cite other memories instead of a run — the
    # evidence list IS their provenance (spec §16.2 reflection)
    cites_evidence = source == "inferred" and bool(payload and payload.get("evidence"))
    if source in MACHINE_SOURCES and run_id is None and not cites_evidence:
        raise MemoryWriteError(f"source '{source}' requires provenance (run_id)")
    if scope == "conversation" and conversation_id is None:
        raise MemoryWriteError("scope 'conversation' requires conversation_id")
    text = " ".join(text.split())
    if not text:
        raise MemoryWriteError("text must not be empty")
    if source == "user_stated":
        # M44 §16.2: the human re-asserting a forgotten fact beats their
        # earlier forget — matching tombstones are removed, the write lands
        await _unforget_by_assertion(text, scope, user_id)

    status = "active"
    review_note: str | None = None
    if kind == "instruction" and (source in {"extracted", "inferred"} or via_tool):
        status = "quarantined"  # behavior-changing writes gate through review
    elif source in MACHINE_SOURCES:
        # M47 §16.2: a kind the extraction tuner routed through review —
        # machine writes only; the human's own words always land directly
        from app.registry_cache import get_cache

        routed = await get_cache().setting("memory_quarantine_kinds") or []
        if kind in routed:
            status = "quarantined"
            review_note = "extraction learner: kind routed through review"

    row = Memory(
        user_id=user_id,
        text=text,
        kind=kind,
        scope=scope,
        source=source,
        status=status,
        review_note=review_note,
        conversation_id=conversation_id,
        project_key=project_key if scope == "project" else None,
        payload=payload,
        entity_key=entity_key,
        importance=max(1, min(10, importance)),
        confidence=max(0.0, min(1.0, confidence)),
        run_id=run_id,
        step_id=step_id,
        supersedes=supersedes,
    )
    if valid_from is not None:
        row.valid_from = valid_from

    async def _write(sess: AsyncSession) -> Memory:
        sess.add(row)
        await sess.flush()
        await _embed_ref(sess, row.id, "memories", row.text)
        await _link_entities(sess, row.id, entities or [])
        await sess.commit()
        await sess.refresh(row)
        return row

    if session is not None:
        result = await _write(session)
    else:
        async with get_session_factory()() as sess:
            result = await _write(sess)
    logger.info(
        "memory_write",
        tier="memory",
        kind=kind,
        source=source,
        status=result.status,
        memory_id=str(result.id),
        run_id=str(run_id) if run_id else None,
    )
    _notify_invalidate()
    return result


async def supersede(
    old_id: UUID,
    *,
    text: str,
    source: str,
    run_id: UUID | None = None,
    step_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    importance: int | None = None,
    confidence: float | None = None,
    valid_from: datetime | None = None,
) -> Memory:
    """Append-only replacement (spec §16.1): insert the new row, close the old
    one bi-temporally in the same transaction. Never deletes."""
    async with get_session_factory()() as session:
        old = await session.get(Memory, old_id)
        if old is None:
            raise MemoryWriteError(f"memory {old_id} not found")
        if old.superseded_at is not None:
            raise MemoryWriteError(f"memory {old_id} is already superseded")
        _validate(old.kind, old.scope, source)
        if source in MACHINE_SOURCES and run_id is None:
            raise MemoryWriteError(f"source '{source}' requires provenance (run_id)")

        now = datetime.now(UTC)
        effective_from = valid_from or now
        new = Memory(
            text=" ".join(text.split()),
            kind=old.kind,
            scope=old.scope,
            conversation_id=old.conversation_id,
            payload=payload if payload is not None else old.payload,
            entity_key=old.entity_key,
            importance=importance if importance is not None else old.importance,
            confidence=confidence if confidence is not None else old.confidence,
            source=source,
            status="active" if old.status in {"active", "expired"} else old.status,
            run_id=run_id,
            step_id=step_id,
            supersedes=old.id,
            pinned=old.pinned,
            half_life_days=old.half_life_days,
        )
        new.valid_from = effective_from
        session.add(new)
        await session.flush()
        # guarded close — no double-supersede even under concurrency
        closed = await session.execute(
            update(Memory)
            .where(Memory.id == old.id, Memory.superseded_at.is_(None))
            .values(
                superseded_at=now,
                superseded_by=new.id,
                valid_to=effective_from,
                status="superseded",
            )
        )
        if int(getattr(closed, "rowcount", 0) or 0) != 1:
            raise MemoryWriteError(f"memory {old_id} was superseded concurrently")
        await _embed_ref(session, new.id, "memories", new.text)
        await session.commit()
        await session.refresh(new)
    logger.info(
        "memory_supersede",
        tier="memory",
        old_id=str(old_id),
        new_id=str(new.id),
        source=source,
    )
    _notify_invalidate()
    return new


async def forget(memory_id: UUID) -> bool:
    """Soft-retire (spec §16.4): status='expired', validity closed. The row
    stays for audit; hard delete is a UI/purge action."""
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        result = await session.execute(
            update(Memory)
            .where(Memory.id == memory_id, Memory.status.in_(("active", "quarantined")))
            .values(status="expired", valid_to=now)
        )
        await session.commit()
    if int(getattr(result, "rowcount", 0) or 0) == 1:
        logger.info("memory_forget", tier="memory", memory_id=str(memory_id))
        _notify_invalidate()
        return True
    return False


# ── M44 §16.1 durable forgetting ──────────────────────────────────────


def normalized_hash(text: str) -> str:
    """SHA-256 of the whitespace-normalized, lowercased text — the exact
    re-admission fingerprint a tombstone keeps instead of the text."""
    import hashlib

    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()


def distinctive_token_hashes(text: str) -> list[str]:
    """Hashes of the text's distinctive tokens (≥6 chars, punctuation
    stripped) — the gray-band anchor for the hybrid suppression gate.
    Live calibration (M44 stage 32) measured real paraphrase pairs at
    cosine 0.876 and 0.847: a lone threshold cannot separate "same fact
    restated" from "same topic, different fact", but true restatements
    share the fact's distinctive payload and value-updates do not."""
    import hashlib
    import re

    out = set()
    for tok in re.split(r"[^a-z0-9\-_.]+", " ".join(text.split()).lower()):
        tok = tok.strip(".-_")
        # distinctive = a payload, not a common word: long, or carrying
        # digits/separators (ids, versions, hosts, emails, bucket names)
        if len(tok) >= 10 or (len(tok) >= 6 and any(c.isdigit() or c in "-_." for c in tok)):
            out.add(hashlib.sha256(tok.encode()).hexdigest()[:16])
    return sorted(out)


async def tombstone_forget(memory_id: UUID) -> bool:
    """FORGET (spec §16.1 M44): delete the row but leave a metadata+hash
    tombstone (and an embedding copy when one exists, for semantic
    suppression only). Requires `memory_forget_enabled`."""
    from app.models import MemoryTombstone

    async with get_session_factory()() as session:
        row = await session.get(Memory, memory_id)
        if row is None:
            return False
        emb = None
        key = await active_model_key()
        if key:
            emb = await session.get(MemoryEmbedding, (memory_id, "memories", key))
        age = 0.0
        if row.recorded_at is not None:
            age = max((datetime.now(UTC) - row.recorded_at).total_seconds() / 86400.0, 0.0)
        session.add(
            MemoryTombstone(
                user_id=row.user_id,
                scope=row.scope,
                project_key=row.project_key,
                conversation_id=row.conversation_id,
                kind=row.kind,
                source=row.source,
                confidence=row.confidence,
                importance=row.importance,
                age_days=age,
                access_count=row.access_count,
                pinned=row.pinned,
                text_hash=normalized_hash(row.text),
                token_hashes=distinctive_token_hashes(row.text),
                embedding=emb.embedding if emb is not None else None,
                model_key=emb.model_key if emb is not None else None,
            )
        )
        await session.commit()
    logger.info("memory_forget", tier="memory", memory_id=str(memory_id))
    return await hard_delete(memory_id)


async def unforget(tombstone_id: UUID, *, reason: str = "user") -> bool:
    """Delete a tombstone — the fact becomes learnable again (§8.8)."""
    from app.models import MemoryTombstone

    async with get_session_factory()() as session:
        row = await session.get(MemoryTombstone, tombstone_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
    logger.info("memory_unforget", tier="memory", tombstone_id=str(tombstone_id), reason=reason)
    return True


async def _matching_tombstones(text: str, scope: str, user_id: UUID | None) -> "list[Any]":
    """Live tombstones this text would re-admit: exact hash match, else
    cosine ≥ memory_forget_similarity against same-model embeddings.
    Tenant- and scope-aware like recall; hash-only when no embedding
    model is configured (§7.4 degradation)."""
    from sqlalchemy import select as sa_select

    from app.models import MemoryTombstone
    from app.registry_cache import get_cache

    owner = (
        MemoryTombstone.user_id == user_id
        if user_id is not None
        else MemoryTombstone.user_id.is_(None)
    )
    text_h = normalized_hash(text)
    async with get_session_factory()() as session:
        exact = list(
            (
                await session.execute(
                    sa_select(MemoryTombstone).where(
                        MemoryTombstone.text_hash == text_h,
                        MemoryTombstone.scope == scope,
                        owner,
                    )
                )
            ).scalars()
        )
    if exact:
        return exact
    key = await active_model_key()
    if not key:
        return []
    try:
        from app.llm import get_embeddings
        from app.registry_cache import get_cache as _gc

        model = await _gc().setting("embedding_model")
        vec = (await get_embeddings(str(model), [" ".join(text.split())]))[0]
    except Exception as exc:  # noqa: BLE001 — degrade to hash-only
        logger.warning("memory_forget_embed_failed", error=str(exc))
        return []
    threshold = float(await get_cache().setting("memory_forget_similarity") or 0.85)
    async with get_session_factory()() as session:
        nearest = list(
            (
                await session.execute(
                    sa_select(
                        MemoryTombstone,
                        MemoryTombstone.embedding.cosine_distance(vec).label("dist"),
                    )
                    .where(
                        MemoryTombstone.embedding.isnot(None),
                        MemoryTombstone.model_key == key,
                        MemoryTombstone.scope == scope,
                        owner,
                    )
                    .order_by(MemoryTombstone.embedding.cosine_distance(vec))
                    .limit(3)
                )
            ).all()
        )
    # the hybrid gate (M44 stage-32 calibration): at or above the threshold
    # cosine alone suppresses; in the gray band down to 0.70 a shared
    # distinctive-token hash is required — true restatements carry the
    # fact's payload token, a changed value does not
    cand_tokens = set(distinctive_token_hashes(text))
    out = []
    for row, dist in nearest:
        cos = 1.0 - float(dist)
        if cos >= threshold or (cos >= 0.70 and cand_tokens & set(row.token_hashes or [])):
            out.append(row)
    return out


async def check_suppressed(text: str, scope: str, user_id: UUID | None) -> bool:
    """§16.2 admission-gate hook: True ⇒ the candidate matches a live
    tombstone and must not be written. Content-free log; the tombstone's
    suppressed_count accrues — the signal a future learner would read."""
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("memory_forget_enabled")):
        return False
    matches = await _matching_tombstones(text, scope, user_id)
    if not matches:
        return False
    async with get_session_factory()() as session:
        for m in matches:
            row = await session.get(type(m), m.id)
            if row is not None:
                row.suppressed_count += 1
                row.last_suppressed_at = datetime.now(UTC)
        await session.commit()
    logger.info(
        "memory_admission_suppressed",
        tier="memory",
        kind=matches[0].kind,
        scope=scope,
        tombstones=len(matches),
    )
    return True


async def _unforget_by_assertion(text: str, scope: str, user_id: UUID | None) -> None:
    """A user re-stating a forgotten fact overrides their earlier forget:
    the matching tombstones are deleted and the write proceeds (§16.2)."""
    from app.registry_cache import get_cache

    if not bool(await get_cache().setting("memory_forget_enabled")):
        return
    for m in await _matching_tombstones(text, scope, user_id):
        await unforget(m.id, reason="user_assertion")


async def hard_delete(memory_id: UUID) -> bool:
    """User/purge-only physical delete (spec §16.1)."""
    async with get_session_factory()() as session:
        row = await session.get(Memory, memory_id)
        if row is None:
            return False
        # unlink chain references so FK constraints allow the delete
        await session.execute(
            update(Memory).where(Memory.supersedes == memory_id).values(supersedes=None)
        )
        await session.execute(
            update(Memory).where(Memory.superseded_by == memory_id).values(superseded_by=None)
        )
        from sqlalchemy import delete as sa_delete

        await session.execute(
            sa_delete(MemoryEmbedding).where(
                MemoryEmbedding.ref_id == memory_id, MemoryEmbedding.table_ref == "memories"
            )
        )
        await session.delete(row)
        await session.commit()
    logger.info("memory_hard_delete", tier="memory", memory_id=str(memory_id))
    _notify_invalidate()
    return True


_CITATION_RE = re.compile(
    r"\s*[\(\[](?:episode|memory|fact|entity|preference|instruction)s?\s+"
    r"[0-9a-f]{8}(?:[,;\s]+[0-9a-f]{8})*[\)\]]",
    re.IGNORECASE,
)


def scrub_citation_ids(text: str) -> str:
    """Strip citation-id parentheticals the extractor may parrot from answers
    — "(episode 1fcf6bb1)", "[fact deadbeef]" — so injected-block ids never
    become memory content (M18 experiment finding: a leaked id in the active
    fact made the planner distrust it and re-verify via fallback)."""
    return _CITATION_RE.sub("", text).strip()


async def gate_candidates(
    candidates: list[Candidate],
) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """Deterministic admission gate (spec §16.2). Returns (accepted, dropped
    with reasons). Near-duplicate detection uses embeddings when available,
    normalized-text equality otherwise."""
    accepted: list[Candidate] = []
    dropped: list[tuple[Candidate, str]] = []

    # M47: the floor is a live setting (default = the old constant,
    # byte-identical) so the §17.7 extraction tuner has a dial to move
    from app.registry_cache import get_cache

    raw_floor = await get_cache().setting("memory_admission_min_confidence")
    floor = float(raw_floor) if raw_floor is not None else GATE_MIN_CONFIDENCE

    seen_texts: set[str] = set()
    for cand in candidates:
        text = scrub_citation_ids(" ".join(cand.text.split()))
        cand.text = text
        norm = text.lower()
        if cand.kind not in KINDS or cand.scope not in SCOPES:
            dropped.append((cand, "invalid kind/scope"))
            continue
        if cand.confidence < floor:
            dropped.append((cand, f"confidence {cand.confidence:.2f} < {floor}"))
            continue
        if not (GATE_MIN_CHARS <= len(text) <= GATE_MAX_CHARS):
            dropped.append((cand, f"length {len(text)} outside bounds"))
            continue
        if norm in seen_texts:
            dropped.append((cand, "duplicate within batch"))
            continue
        # near-duplicate vs the ACTIVE store
        dup = await _near_duplicate(text, cand.scope, cand.kind, cand.conversation_id)
        if dup is not None:
            dropped.append((cand, f"near-duplicate of {dup}"))
            continue
        seen_texts.add(norm)
        cand.text = text
        accepted.append(cand)
    if dropped:
        logger.info(
            "memory_gate_dropped",
            tier="memory",
            dropped=len(dropped),
            reasons=[r for _, r in dropped],
        )
    return accepted, dropped


async def _near_duplicate(
    text: str, scope: str, kind: str, conversation_id: UUID | None
) -> UUID | None:
    """True-cosine near-duplicate check against the top hybrid hit (the RRF
    relevance is rank-normalized — the top hit is always 1.0 — so it must
    never gate); exact normalized-text equality is the lexical fallback."""
    from app.memory.rank import recall

    hits = await recall(
        text,
        scopes=[scope],
        kinds=[kind],
        conversation_id=conversation_id,
        k=1,
        floor=0.0,
        bump_access=False,
    )
    if not hits:
        return None
    top = hits[0]
    if top.memory.text.lower() == text.lower():
        return top.memory.id
    try:
        from app.retrieval import _query_vector, cosine

        qvec = await _query_vector(text)
        if qvec is None:
            return None
        key = await active_model_key()
        if key is None:
            return None
        async with get_session_factory()() as session:
            row = await session.get(MemoryEmbedding, (top.memory.id, "memories", key))
        if row is not None and cosine(qvec, list(row.embedding)) >= GATE_DUP_COSINE:
            return top.memory.id
    except Exception as exc:  # noqa: BLE001 — the gate stays deterministic-safe
        logger.warning("memory_dup_check_failed", error=str(exc))
    return None


async def list_memories(
    *,
    scope: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    source: str | None = None,
    conversation_id: UUID | None = None,
    q: str | None = None,
    limit: int = 100,
) -> list[Memory]:
    from app.auth import scope_to_user

    async with get_session_factory()() as session:
        stmt = scope_to_user(select(Memory).order_by(Memory.recorded_at.desc()), Memory).limit(
            limit
        )
        if scope:
            stmt = stmt.where(Memory.scope == scope)
        if kind:
            stmt = stmt.where(Memory.kind == kind)
        if status:
            stmt = stmt.where(Memory.status == status)
        if source:
            stmt = stmt.where(Memory.source == source)
        if conversation_id:
            stmt = stmt.where(Memory.conversation_id == conversation_id)
        if q:
            stmt = stmt.where(Memory.text.ilike(f"%{q}%"))
        return list((await session.execute(stmt)).scalars())


def _notify_invalidate() -> None:
    """Fire the §7.3 NOTIFY discipline for the memory store (cache-hint only;
    tables are the truth). Best-effort by design."""
    try:
        from app.registry_cache import get_cache

        cache = get_cache()
        notify = getattr(cache, "notify_memory_dirty", None)
        if callable(notify):  # pragma: no cover - wired when a memory cache exists
            notify()
    except Exception:  # noqa: BLE001, S110 - invalidation must never fail a write
        pass
