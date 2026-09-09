"""Routines API (spec §17.2/§17.4 — M20 substrate).

CRUD + fire-token lifecycle + the webhook fire endpoint. Fire tokens are
generated server-side, returned ONCE, and stored as SHA-256 hashes. The fire
payload is stored verbatim as UNTRUSTED event input — it can start a run
(from M22), never steer one.
"""

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.auth import current_user_id, owns_row, scope_to_user
from app.models import Routine
from app.models.ambient import ROUTINE_AUTONOMY

router = APIRouter(prefix="/routines", tags=["routines"])


class RoutineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1)
    description: str | None = None
    triggers: list[dict[str, Any]] | None = None
    allowlist: dict[str, Any] | None = None
    model_ref: str | None = None
    include_memories: bool = False
    autonomy: str = "propose"
    budgets: dict[str, Any] | None = None


class RoutinePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    prompt: str | None = None
    description: str | None = None
    triggers: list[dict[str, Any]] | None = None
    allowlist: dict[str, Any] | None = None
    model_ref: str | None = None
    include_memories: bool | None = None
    autonomy: str | None = None
    budgets: dict[str, Any] | None = None


def _out(r: Routine) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": r.name,
        "description": r.description,
        "prompt": r.prompt,
        "source": r.source,
        "triggers": r.triggers,
        "allowlist": r.allowlist,
        "model_ref": r.model_ref,
        "include_memories": r.include_memories,
        "autonomy": r.autonomy,
        "budgets": r.budgets,
        "has_fire_token": r.fire_token_hash is not None,
        "stagger_offset_s": r.stagger_offset_s,
        "status": r.status,
        "status_reason": r.status_reason,
        "consecutive_failures": r.consecutive_failures,
        "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _ambient_on(session: SessionDep) -> None:
    from app.registry_cache import get_cache

    if not await get_cache().setting("ambient_enabled"):
        raise HTTPException(409, "ambient mode is disabled (ambient_enabled=false)")


def _validate_model_ref(ref: str | None) -> None:
    """§18.1: a routine's model override is validated at save time, exactly
    like the settings model refs."""
    if not ref:
        return
    from app.llm.registry import validate_model_selection

    errors = validate_model_selection(ref)
    if errors:
        raise HTTPException(422, f"model_ref: {'; '.join(errors)}")


@router.get("")
async def list_routines(session: SessionDep) -> list[dict[str, Any]]:
    rows = list(
        (
            await session.execute(scope_to_user(select(Routine).order_by(Routine.name), Routine))
        ).scalars()
    )
    return [_out(r) for r in rows]


@router.post("", status_code=201)
async def create_routine(body: RoutineBody, session: SessionDep) -> dict[str, Any]:
    await _ambient_on(session)
    from app.registry_cache import get_cache

    if body.autonomy not in ROUTINE_AUTONOMY:
        raise HTTPException(422, f"autonomy must be one of {sorted(ROUTINE_AUTONOMY)}")
    _validate_model_ref(body.model_ref)
    cap = int(await get_cache().setting("ambient_max_routines"))
    count = (await session.execute(select(func.count()).select_from(Routine))).scalar_one()
    if count >= cap:
        raise HTTPException(409, f"ambient_max_routines cap reached ({cap})")
    dup = (
        await session.execute(select(Routine).where(Routine.name == body.name))
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"routine name {body.name!r} already exists")
    row = Routine(
        user_id=current_user_id(),
        name=body.name,
        prompt=body.prompt,
        description=body.description,
        triggers=body.triggers,
        allowlist=body.allowlist,
        model_ref=body.model_ref,
        include_memories=body.include_memories,
        autonomy=body.autonomy,
        budgets=body.budgets,
        stagger_offset_s=secrets.randbelow(300),  # consistent per-routine stagger
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.get("/{routine_id}")
async def get_routine(routine_id: UUID, session: SessionDep) -> dict[str, Any]:
    row = await session.get(Routine, routine_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "routine not found")
    return _out(row)


@router.patch("/{routine_id}")
async def patch_routine(
    routine_id: UUID, body: RoutinePatch, session: SessionDep
) -> dict[str, Any]:
    row = await session.get(Routine, routine_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "routine not found")
    changes = body.model_dump(exclude_none=True)
    if row.source == "static":
        # §4 discipline: static definitions immutable — status toggles only
        illegal = set(changes) - {"status"}
        if illegal:
            raise HTTPException(
                409, f"static routine: only status may change (got {sorted(illegal)})"
            )
    if "autonomy" in changes and changes["autonomy"] not in ROUTINE_AUTONOMY:
        raise HTTPException(422, f"autonomy must be one of {sorted(ROUTINE_AUTONOMY)}")
    if "model_ref" in changes:
        _validate_model_ref(changes["model_ref"])
    if "status" in changes and changes["status"] not in {"active", "paused"}:
        raise HTTPException(422, "status must be 'active' or 'paused'")
    for key, value in changes.items():
        setattr(row, key, value)
    if changes.get("status") == "active":
        row.consecutive_failures = 0
        row.status_reason = None
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.delete("/{routine_id}", status_code=204)
async def delete_routine(routine_id: UUID, session: SessionDep) -> None:
    row = await session.get(Routine, routine_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "routine not found")
    if row.source == "static":
        raise HTTPException(409, "static routines cannot be deleted")
    await session.delete(row)
    await session.commit()


# ── fire-token lifecycle (spec §17.2) ────────────────────────────────


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/{routine_id}/token")
async def issue_token(routine_id: UUID, session: SessionDep) -> dict[str, str]:
    """Issue (or rotate) the fire token. Shown ONCE — only the hash is kept."""
    await _ambient_on(session)
    row = await session.get(Routine, routine_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "routine not found")
    token = f"amb_{secrets.token_urlsafe(32)}"
    row.fire_token_hash = _hash_token(token)
    await session.commit()
    return {"fire_token": token, "note": "shown once — store it now"}


@router.delete("/{routine_id}/token", status_code=204)
async def revoke_token(routine_id: UUID, session: SessionDep) -> None:
    row = await session.get(Routine, routine_id)
    if row is None or not owns_row(row):
        raise HTTPException(404, "routine not found")
    row.fire_token_hash = None
    await session.commit()


class FireBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=65536)
    payload: dict[str, Any] | None = None
    dedupe_key: str | None = Field(default=None, max_length=255)


@router.post("/{routine_id}/fire", status_code=202)
async def fire_routine(
    routine_id: UUID,
    body: FireBody,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """The webhook fire path: bearer token → UNTRUSTED event → drain pickup.
    A leaked token can start a run, never steer one (§17.2). No owner check
    here — the fire token IS the auth (§18.8 exempts this path)."""
    await _ambient_on(session)
    row = await session.get(Routine, routine_id)
    if row is None:
        raise HTTPException(404, "routine not found")
    if row.status != "active":
        raise HTTPException(409, f"routine is {row.status}")
    if row.fire_token_hash is None:
        raise HTTPException(401, "no fire token issued for this routine")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or _hash_token(token) != row.fire_token_hash:
        raise HTTPException(401, "invalid fire token")

    from app.ambient.store import ChainGuardError, emit_event

    try:
        event = await emit_event(
            kind="routine_fire",
            source="webhook",
            payload={"text": body.text, "payload": body.payload},  # UNTRUSTED
            dedupe_key=body.dedupe_key,
            routine_id=row.id,
        )
    except ChainGuardError as exc:
        raise HTTPException(429, str(exc)) from exc
    if event is None:
        return {"status": "deduplicated"}
    row.last_fired_at = datetime.now(UTC)
    await session.commit()
    return {"status": "accepted", "event_id": str(event.id)}


# ── presence heartbeat (spec §17.5) ──────────────────────────────────

presence_router = APIRouter(prefix="/presence", tags=["presence"])


class HeartbeatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool = True
    activity: bool = False


@presence_router.post("/heartbeat")
async def presence_heartbeat(body: HeartbeatBody, session: SessionDep) -> dict[str, Any]:
    from app.registry_cache import get_cache

    if not await get_cache().setting("ambient_enabled"):
        return {"state": "disabled"}  # byte-identity: no writes while dark
    from app.ambient.presence import record_heartbeat

    row = await record_heartbeat(visible=body.visible, activity=body.activity)
    return {"state": row.state, "visible": row.visible}


@presence_router.get("")
async def presence_state(session: SessionDep) -> dict[str, Any]:
    from app.models import UserPresence

    row = await session.get(UserPresence, "default")
    if row is None:
        return {"state": "offline"}
    return {
        "state": row.state,
        "visible": row.visible,
        "last_activity_at": row.last_activity_at.isoformat() if row.last_activity_at else None,
    }
