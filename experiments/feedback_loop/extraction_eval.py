"""M47 harness (closure_pack): the extraction-admission loop replayed
through the SHIPPED pipeline — real `gate_candidates` (live floor), real
`remember()` (live kind router), real `tombstone_forget`, real quarantine
review transitions, real tuner — deterministic on the fake provider,
key-free.

What the M44 forget gate CANNOT do frames this experiment: tombstones
suppress REPEATS of a forgotten fact; they do nothing about NOVEL junk.
Two worlds, one per tuner lever:

WORLD A — kind-concentrated junk (the router's case):
  preference  valuable (forget 5%),  confidence 0.72–0.92
  fact        mixed    (forget 30%), confidence 0.60–0.90
  entity      junk     (forget 90%), confidence 0.55–0.75
No static floor separates entity junk (up to .75) from preference
(from .72) — the kind router must.

WORLD B — cross-kind low-confidence junk (the floor's case): the same
three kinds at confidence 0.50–0.85, where anything below 0.62 is junk
(forgotten) regardless of kind and the rest is 5%-forgotten. No kind's
overall repudiation crosses the routing rate — only the band-walking
floor can refuse this stream.

THE EVIDENCE BAR — set before the learner existed (FLE-1 §4 shape):
  1. learner junk admissions strictly below every static floor that
     holds valuable-blocked at the shipped default's level, per world;
  2. learner valuable-blocked no worse than the shipped default in
     world A; in world B, strictly below the junk reduction it buys;
  3. zero clamp violations (floor outside [0.5, 0.9], or any routing
     of a user_stated write);
  4. the report keeps whatever argues AGAINST shipping visible.

Usage: PYTHONPATH=backend python experiments/feedback_loop/extraction_eval.py
       (writes result_extraction.json beside this file)
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test"
)
os.environ.setdefault("FAKE_LLM_ENABLED", "1")

ROUNDS = 30
PER_ROUND = 2  # candidates per kind per round

# a world: kind -> (confidence pattern, repudiate(confidence, n), junk(confidence))
WorldSpec = dict[str, tuple[Callable[[int], float], Callable[[float, int], bool], Callable[[float], bool]]]

WORLD_A: WorldSpec = {
    "preference": (lambda i: 0.72 + 0.02 * (i % 11), lambda c, n: n % 20 == 0, lambda c: False),
    "fact": (lambda i: 0.60 + 0.03 * (i % 11), lambda c, n: n % 10 < 3, lambda c: False),
    "entity": (lambda i: 0.55 + 0.02 * (i % 11), lambda c, n: n % 10 != 0, lambda c: True),
}
_B_JUNK_BELOW = 0.62
WORLD_B: WorldSpec = {
    kind: (
        conf_fn,
        lambda c, n: c < _B_JUNK_BELOW or n % 20 == 0,
        lambda c: c < _B_JUNK_BELOW,
    )
    for kind, conf_fn in {
        "preference": lambda i: 0.55 + 0.03 * (i % 11),  # 0.55..0.85
        "fact": lambda i: 0.52 + 0.03 * (i % 11),  # 0.52..0.82
        "entity": lambda i: 0.50 + 0.03 * (i % 11),  # 0.50..0.80
    }.items()
}
VOCAB = "alpha bravo charlie delta echo foxtrot golf hotel india juliet".split()


def _text(kind: str, i: int) -> str:
    # distinct facts — never paraphrases, so the M44 gate stays out of frame
    return f"{kind} item {VOCAB[i % 10]} {VOCAB[(i * 3 + 1) % 10]} n{i:03d}"


async def _reset() -> None:
    from sqlalchemy import text

    from app.db import get_engine
    from app.models import Base
    from app.registry_cache import reset_cache

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    reset_cache()


async def _world_run_id() -> Any:
    from app.db import get_session_factory
    from app.models import Conversation, Run

    async with get_session_factory()() as session:
        conv = Conversation(title="extraction eval")
        session.add(conv)
        await session.flush()
        run = Run(
            conversation_id=conv.id,
            chat_message="eval",
            status="completed",
            trigger={"kind": "eval"},
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
    return run.id


async def _admit_round(
    world: WorldSpec, i: int, run_id: Any, counters: dict[str, Any]
) -> None:
    """One round of machine extraction through the SHIPPED admission path:
    gate_candidates (live floor) then remember() (live kind router)."""
    from app.memory.store import Candidate, MemoryWriteError, gate_candidates, remember

    cands = [
        Candidate(
            text=_text(kind, i * PER_ROUND + j),
            kind=kind,
            scope="global",
            confidence=round(conf_fn(i * PER_ROUND + j), 2),
            importance=5,
        )
        for kind, (conf_fn, _, _) in world.items()
        for j in range(PER_ROUND)
    ]
    accepted, dropped = await gate_candidates(cands)
    for cand, reason in dropped:
        if not reason.startswith("confidence"):
            continue
        junk_fn = world[cand.kind][2]
        if not junk_fn(cand.confidence):
            counters["valuable_blocked"] += 1
    for cand in accepted:
        try:
            await remember(
                text=cand.text,
                kind=cand.kind,
                scope=cand.scope,
                source="extracted",
                confidence=cand.confidence,
                importance=cand.importance,
                run_id=run_id,
            )
        except MemoryWriteError:
            pass


async def _scripted_human(
    world: WorldSpec, judged: set[str], counters: dict[str, Any]
) -> None:
    """Ground truth acting through the SHIPPED verbs: forget via
    tombstone_forget, quarantine review via the same status transition the
    review API performs."""
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.memory.store import tombstone_forget
    from app.models import Memory

    seen_per_kind: dict[str, int] = counters.setdefault("_seen", {})
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(Memory).where(Memory.source == "extracted").order_by(Memory.recorded_at)
                )
            ).scalars()
        )
    for row in rows:
        rid = str(row.id)
        if rid in judged or row.status not in ("active", "quarantined"):
            continue
        _, repudiate_fn, junk_fn = world[row.kind]
        n = seen_per_kind.get(row.kind, 0)
        conf = float(row.confidence)
        repudiate = repudiate_fn(conf, n)
        judged.add(rid)
        seen_per_kind[row.kind] = n + 1
        if row.status == "active":
            if junk_fn(conf):
                counters["junk_admitted"] += 1
            if repudiate:
                await tombstone_forget(row.id)
        else:  # quarantined — scripted review, same transition as the API
            if not junk_fn(conf) and not repudiate:
                counters["valuable_quarantined"] += 1
            async with get_session_factory()() as session:
                fresh = await session.get(Memory, row.id)
                if fresh is not None:
                    fresh.status = "rejected" if repudiate else "active"
                    fresh.review_note = "eval scripted review"
                    await session.commit()


async def _summarize(condition: dict[str, Any], counters: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import Memory, MemoryTombstone

    async with get_session_factory()() as session:
        rows = list((await session.execute(select(Memory))).scalars())
        stones = list((await session.execute(select(MemoryTombstone))).scalars())
    kept = sum(1 for r in rows if r.status == "active" and r.source == "extracted")
    repudiated = len(stones) + sum(
        1 for r in rows if r.status == "rejected" and r.source == "extracted"
    )
    total_judged = kept + repudiated
    return condition | {
        "junk_admitted": counters["junk_admitted"],
        "valuable_blocked": counters["valuable_blocked"],
        "valuable_quarantined": counters["valuable_quarantined"],
        "kept_precision": round(kept / total_judged, 3) if total_judged else None,
        "tombstones": len(stones),
        "rejected_in_review": sum(1 for r in rows if r.status == "rejected"),
    }


async def _configure(floor: float, learning: str) -> None:
    from app.db import get_session_factory
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "memory_enabled": True,
                "memory_forget_enabled": True,
                "memory_admission_min_confidence": floor,
                "memory_extraction_learning": learning,
                "embedding_model": "fake:scripted",
                "default_model": "fake:scripted",
            },
        )


def _fresh_counters() -> dict[str, Any]:
    return {"junk_admitted": 0, "valuable_blocked": 0, "valuable_quarantined": 0}


async def run_static(world: WorldSpec, label: str, floor: float) -> dict[str, Any]:
    """Baseline condition: fixed admission floor, learning off."""
    await _reset()
    await _configure(floor, "off")
    run_id = await _world_run_id()
    judged: set[str] = set()
    counters = _fresh_counters()
    for i in range(ROUNDS):
        await _admit_round(world, i, run_id, counters)
        await _scripted_human(world, judged, counters)
    return await _summarize({"world": label, "mode": "static", "floor": floor}, counters)


async def run_learner(world: WorldSpec, label: str, start_floor: float) -> dict[str, Any]:
    """The M47 tuner in auto, every 5 rounds, identical stream."""
    from app.memory.extract_learn import run_extraction_tuner
    from app.registry_cache import get_cache

    await _reset()
    await _configure(start_floor, "auto")
    run_id = await _world_run_id()
    judged: set[str] = set()
    counters = _fresh_counters()
    floor_track = [start_floor]
    clamp_violations = 0
    for i in range(ROUNDS):
        await _admit_round(world, i, run_id, counters)
        await _scripted_human(world, judged, counters)
        if (i + 1) % 5 == 0:
            await run_extraction_tuner(force=True)
            floor = float(await get_cache().setting("memory_admission_min_confidence"))
            floor_track.append(floor)
            if not 0.5 <= floor <= 0.9:
                clamp_violations += 1
    routed = list(await get_cache().setting("memory_quarantine_kinds") or [])
    out = await _summarize(
        {"world": label, "mode": "learner_auto", "start_floor": start_floor}, counters
    )
    return out | {
        "floor_track": floor_track,
        "clamp_violations": clamp_violations,
        "quarantined_kinds": sorted(routed),
    }


async def main() -> None:
    from app.llm.registry import register_builtin_providers

    register_builtin_providers()
    out: dict[str, Any] = {
        "world_a": {
            "baseline_floor_sweep": [
                await run_static(WORLD_A, "A", f) for f in (0.5, 0.6, 0.7, 0.75)
            ],
            "learner": await run_learner(WORLD_A, "A", 0.5),
        },
        "world_b": {
            "baseline_floor_sweep": [
                await run_static(WORLD_B, "B", f) for f in (0.5, 0.6, 0.65)
            ],
            "learner": await run_learner(WORLD_B, "B", 0.5),
        },
    }
    path = Path(__file__).with_name("result_extraction.json")
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
