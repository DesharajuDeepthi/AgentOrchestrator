"""Memory native tools (spec §16.4) — registry citizens.

Seeded static, hidden by default; the `memory-keeper` skill binds all three.
Exposure decides which loops may call them; §3.3 boundaries unchanged. Every
call is an ordinary tool_call step with §10 labels.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from app.native.provider import native_tool


def _memory_dict(mem: Any, score: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(mem.id),
        "text": mem.text,
        "kind": mem.kind,
        "scope": mem.scope,
        "status": mem.status,
        "source": mem.source,
        "importance": mem.importance,
        "valid_from": mem.valid_from.isoformat() if mem.valid_from else None,
        "valid_to": mem.valid_to.isoformat() if mem.valid_to else None,
        "pinned": bool(mem.pinned),
    }
    if score is not None:
        out["score"] = score
    return out


@native_tool(
    "memory.recall",
    "Search the agent's long-term memory (facts, preferences, entities, past "
    "instructions). Returns scored matches with ids. Optional: kinds filter "
    "(fact|preference|entity|relation|instruction), scope (global|conversation), "
    "as_of (ISO timestamp — what was believed/true at that time). If nothing "
    "relevant is stored, an empty list is returned — never invent a memory.",
)
async def memory_recall(
    query: str,
    kinds: list[str] | None = None,
    scope: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    from app.memory import recall
    from app.registry_cache import get_cache

    cache = get_cache()
    if not await cache.setting("memory_enabled"):
        return {"memories": [], "note": "memory is disabled (memory_enabled=false)"}
    top_k = int(await cache.setting("memory_recall_top_k"))
    floor = float(await cache.setting("memory_score_floor"))
    as_of_dt: datetime | None = None
    if as_of:
        as_of_dt = datetime.fromisoformat(as_of)
    hits = await recall(
        query,
        scopes=[scope] if scope else None,
        kinds=kinds,
        conversation_id=_current_conversation_id(),
        k=top_k,
        floor=floor,
        as_of=as_of_dt,
    )
    return {"memories": [_memory_dict(h.memory, h.score) for h in hits]}


@native_tool(
    "memory.remember",
    "Store one durable memory the user has stated or clearly implied "
    "(kind: fact | preference | entity | relation | instruction). Keep it one "
    "atomic statement. instruction-kind memories are quarantined for human "
    "review before they take effect. Scope 'conversation' keeps it local to "
    "this conversation; default 'global'.",
)
async def memory_remember(
    text: str,
    kind: str = "fact",
    scope: str = "global",
    importance: int = 5,
    project: str | None = None,
) -> dict[str, Any]:
    from app.memory import MemoryWriteError, remember
    from app.registry_cache import get_cache

    if not await get_cache().setting("memory_enabled"):
        return {"stored": False, "note": "memory is disabled (memory_enabled=false)"}
    ctx = _current_run_context()
    try:
        row = await remember(
            text=text,
            kind=kind,
            scope=scope,
            source="user_stated",
            conversation_id=_current_conversation_id(),
            project_key=project,
            importance=importance,
            run_id=ctx[0],
            step_id=ctx[1],
            via_tool=True,
        )
    except MemoryWriteError as exc:
        return {"stored": False, "error": str(exc)}
    note = None
    if row.status == "quarantined":
        note = "instruction memories require human approval before they take effect"
    return {"stored": True, "memory": _memory_dict(row), "note": note}


@native_tool(
    "memory.forget",
    "Retire one memory by id (from memory.recall results). The row is expired, "
    "not destroyed — a human can still audit or restore it from the Memory page.",
)
async def memory_forget(memory_id: str) -> dict[str, Any]:
    from app.memory import forget
    from app.registry_cache import get_cache

    if not await get_cache().setting("memory_enabled"):
        return {"forgotten": False, "note": "memory is disabled (memory_enabled=false)"}
    try:
        target = UUID(memory_id)
    except ValueError:
        return {"forgotten": False, "error": f"'{memory_id}' is not a memory id"}
    ok = await forget(target)
    return (
        {"forgotten": ok}
        if ok
        else {
            "forgotten": False,
            "error": "no active or quarantined memory with that id",
        }
    )


def _current_run_context() -> tuple[UUID | None, UUID | None]:
    """Provenance from the ambient run context when a loop calls the tool."""
    from app.orchestrator.context import get_run_context

    ctx = get_run_context()
    if ctx is None:
        return None, None
    return ctx.run_id, None


def _current_conversation_id() -> UUID | None:
    from app.orchestrator.context import get_run_context

    ctx = get_run_context()
    return ctx.conversation_id if ctx else None
