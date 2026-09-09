"""L2 write pipeline (spec §16.2) — extract → gate → reconcile → resolve.

The division of labor is the strongest result in the evaluation literature
(research 03 §7): the LLM answers ONLY the narrow identity question ("same
fact / related / unrelated"); deterministic code resolves winners by event
time. Extracted/inferred instruction-kind candidates never supersede —
they quarantine through remember() and activate only via human review.
"""

from typing import Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from app.db import get_session_factory
from app.memory.rank import recall
from app.memory.store import Candidate, gate_candidates, remember, supersede
from app.models import Memory, Run, RunStep

logger = structlog.get_logger("memory")

_RECONCILE_NEIGHBORS = 4


class ExtractedMemory(BaseModel):
    text: str = Field(description="one atomic sentence, 8-300 chars")
    kind: Literal["fact", "preference", "entity", "relation", "instruction"]
    importance: int = Field(ge=1, le=10)
    confidence: float = Field(ge=0.0, le=1.0)
    entity_key: str | None = Field(
        default=None, description="single-valued key this memory is about, else null"
    )
    entities: list[str] = Field(
        default_factory=list,
        description="0-3 named entities this memory is about (people, systems, projects)",
    )


class ExtractionOutput(BaseModel):
    memories: list[ExtractedMemory] = Field(default_factory=list)


class NeighborVerdict(BaseModel):
    index: int = Field(description="the existing memory's number in the list")
    verdict: Literal["same", "related", "unrelated"]


class ReconcileOutput(BaseModel):
    verdicts: list[NeighborVerdict] = Field(default_factory=list)


async def _extraction_model() -> tuple[str, object]:
    from app.llm import ModelParams, get_model
    from app.registry_cache import get_cache

    cache = get_cache()
    ref = await cache.setting("memory_extraction_model") or await cache.setting("default_model")
    raw_params = await cache.setting("memory_extraction_model_params")
    params = ModelParams.model_validate(raw_params) if raw_params else ModelParams(effort="low")
    return str(ref), get_model(str(ref), params)


async def extract_candidates(run: Run, hitl_notes: list[str]) -> list[Candidate]:
    """One structured-output call; failure means zero candidates (fail-open)."""
    from app.prompts import load_prompt

    try:
        _, model = await _extraction_model()
        structured = model.with_structured_output(ExtractionOutput)  # type: ignore[attr-defined]
        prompt = load_prompt("memory_extract").format(
            task=(run.chat_message or "")[:2000],
            answer=(run.final_answer or "")[:2000],
            status=run.status,
            hitl_notes="\n".join(f"- {n}" for n in hitl_notes) or "(none)",
        )
        out = await structured.ainvoke(prompt)
        assert isinstance(out, ExtractionOutput)
    except Exception as exc:  # noqa: BLE001 — extraction never fails the pipeline
        logger.info("memory_extract_failed", run_id=str(run.id), error=str(exc))
        return []
    return [
        Candidate(
            text=m.text,
            kind=m.kind,
            importance=m.importance,
            confidence=m.confidence,
            entity_key=m.entity_key,
            entities=m.entities,
        )
        for m in out.memories
    ]


async def _judge_neighbors(cand: Candidate, neighbors: list[Memory]) -> dict[UUID, str]:
    """LLM identity verdicts per neighbor; on failure everything is 'related'
    (safe: related ⇒ ADD, never a wrong supersede)."""
    from app.prompts import load_prompt

    try:
        _, model = await _extraction_model()
        structured = model.with_structured_output(ReconcileOutput)  # type: ignore[attr-defined]
        listing = "\n".join(f"{i + 1}. {m.text}" for i, m in enumerate(neighbors))
        prompt = load_prompt("memory_reconcile").format(candidate=cand.text, neighbors=listing)
        out = await structured.ainvoke(prompt)
        assert isinstance(out, ReconcileOutput)
    except Exception as exc:  # noqa: BLE001
        logger.info("memory_reconcile_failed", error=str(exc))
        return {m.id: "related" for m in neighbors}
    verdicts: dict[UUID, str] = {m.id: "related" for m in neighbors}
    for v in out.verdicts:
        if 1 <= v.index <= len(neighbors):
            verdicts[neighbors[v.index - 1].id] = v.verdict
    return verdicts


def _candidate_owner() -> "UUID | None":
    """§18.8: extraction runs in the requester's context when there is one."""
    from app.auth import current_user_id

    return current_user_id()


async def reconcile_and_write(cand: Candidate, run_id: UUID) -> Memory | None:
    """Resolve one gated candidate against the active store.

    Deterministic rules (spec §16.2):
    - instruction candidates: always remember() → quarantine (no supersede)
    - entity_key match on an active row: same fact by construction → supersede
    - otherwise ask the LLM for identity verdicts on hybrid neighbors:
      'same' → supersede the matched row (extraction is post-run ⇒ candidate
      carries the newest event time); no 'same' → plain add
    """
    from app.memory.store import check_suppressed

    if await check_suppressed(cand.text, cand.scope, _candidate_owner()):
        return None  # M44 §16.2: the user forgot this — do not re-learn it

    if cand.kind == "instruction":
        return await remember(
            text=cand.text,
            kind=cand.kind,
            scope=cand.scope,
            source="extracted",
            conversation_id=cand.conversation_id,
            importance=cand.importance,
            confidence=cand.confidence,
            entity_key=cand.entity_key,
            run_id=run_id,
        )

    # deterministic same-fact shortcut: an entity_key names the fact identity
    if cand.entity_key:
        from sqlalchemy import select

        async with get_session_factory()() as session:
            match = (
                (
                    await session.execute(
                        select(Memory).where(
                            Memory.entity_key == cand.entity_key,
                            Memory.status == "active",
                            Memory.scope == cand.scope,
                        )
                    )
                )
                .scalars()
                .first()
            )
        if match is not None:
            if match.text.lower() == cand.text.lower():
                return None  # NOOP
            return await supersede(
                match.id,
                text=cand.text,
                source="extracted",
                run_id=run_id,
                importance=cand.importance,
                confidence=cand.confidence,
            )

    hits = await recall(
        cand.text,
        scopes=[cand.scope],
        kinds=[cand.kind],
        conversation_id=cand.conversation_id,
        k=_RECONCILE_NEIGHBORS,
        floor=0.0,
        bump_access=False,
    )
    neighbors = [h.memory for h in hits]
    if neighbors:
        verdicts = await _judge_neighbors(cand, neighbors)
        same = [m for m in neighbors if verdicts.get(m.id) == "same"]
        if same:
            # newest active 'same' row is the one the candidate replaces
            target = max(same, key=lambda m: m.valid_from)
            if target.text.lower() == cand.text.lower():
                return None  # NOOP — identical restatement
            return await supersede(
                target.id,
                text=cand.text,
                source="extracted",
                run_id=run_id,
                importance=cand.importance,
                confidence=cand.confidence,
            )
    return await remember(
        text=cand.text,
        kind=cand.kind,
        scope=cand.scope,
        source="extracted",
        conversation_id=cand.conversation_id,
        payload=cand.payload,
        entity_key=cand.entity_key,
        importance=cand.importance,
        confidence=cand.confidence,
        run_id=run_id,
        entities=cand.entities,
    )


async def extract_from_run(run_id: UUID) -> list[Memory]:
    """The full §16.2 write pipeline for one completed run."""
    from sqlalchemy import select

    from app.registry_cache import get_cache

    cache = get_cache()
    if not await cache.setting("memory_enabled"):
        return []
    if not await cache.setting("memory_extraction_enabled"):
        return []
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        if run is None or run.status != "completed":
            return []
        hitl_steps = list(
            (
                await session.execute(
                    select(RunStep).where(RunStep.run_id == run_id, RunStep.step_type == "hitl")
                )
            ).scalars()
        )
    hitl_notes = [
        f"{(s.output or {}).get('status')}: {(s.output or {}).get('note')}"
        for s in hitl_steps
        if s.output and (s.output or {}).get("note")
    ]
    candidates = await extract_candidates(run, hitl_notes)
    if not candidates:
        return []
    accepted, dropped = await gate_candidates(candidates)
    written: list[Memory] = []
    for cand in accepted:
        try:
            row = await reconcile_and_write(cand, run_id)
        except Exception as exc:  # noqa: BLE001 — candidates are independent
            logger.warning("memory_reconcile_write_failed", error=str(exc))
            continue
        if row is not None:
            written.append(row)
    from app import obs

    obs.MEMORY_OPS.labels(kind="extract", status="ok").inc()
    logger.info(
        "memory_extract",
        tier="memory",
        kind="extract",
        run_id=str(run_id),
        candidates=len(candidates),
        gated_out=len(dropped),
        written=len(written),
    )
    return written


async def post_run_extract(run_id: UUID) -> None:
    """Scheduler chain entry (POST_RUN_EXTRA)."""
    await extract_from_run(run_id)
