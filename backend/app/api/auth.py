"""Auth API (spec §18.8): login, me, logout, admin user management, and
self-service prefs (per-user ambient overrides). All 404 when auth is
dark? No — the router exists but every endpoint 409s while auth is off,
mirroring the ambient dark pattern (§17): the surface is discoverable,
inert, and byte-identity holds because nothing else consults it."""

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import SessionDep
from app.auth import (
    auth_enabled,
    create_session,
    hash_password,
    revoke_session,
    verify_password,
)
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])

PREF_KEYS = {"ambient_quiet_hours", "ambient_digest_times"}


def _require_on() -> None:
    if not auth_enabled():
        raise HTTPException(409, "auth is disabled (AUTH_ENABLED=false)")


def _bearer(authorization: str | None) -> str:
    return (authorization or "").removeprefix("Bearer ").strip()


async def _require_user(session: Any, authorization: str | None) -> User:
    from app.auth import authenticate

    info = await authenticate(_bearer(authorization))
    if info is None:
        raise HTTPException(401, "authentication required")
    user: User | None = await session.get(User, info["id"])
    if user is None:
        raise HTTPException(401, "authentication required")
    return user


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody, session: SessionDep) -> dict[str, Any]:
    _require_on()
    user = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid username or password")
    token, expires = await create_session(user)
    return {
        "token": token,
        "username": user.username,
        "role": user.role,
        "expires_at": expires.isoformat(),
    }


@router.post("/logout", status_code=204)
async def logout(authorization: str | None = Header(default=None)) -> None:
    _require_on()
    await revoke_session(_bearer(authorization))


@router.get("/me")
async def me(
    session: SessionDep, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_on()
    user = await _require_user(session, authorization)
    return {
        "id": str(user.id),
        "username": user.username,
        "role": user.role,
        "prefs": user.prefs or {},
    }


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    role: str = "member"


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreate, session: SessionDep, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_on()
    actor = await _require_user(session, authorization)
    if actor.role != "admin":
        raise HTTPException(403, "user management requires the admin role")
    if body.role not in {"admin", "member"}:
        raise HTTPException(422, "role must be admin | member")
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    exists = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, f"username {body.username!r} already exists")
    user = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {"id": str(user.id), "username": user.username, "role": user.role}


@router.get("/users")
async def list_users(
    session: SessionDep, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_on()
    actor = await _require_user(session, authorization)
    if actor.role != "admin":
        raise HTTPException(403, "user management requires the admin role")
    rows = list((await session.execute(select(User).order_by(User.created_at))).scalars())
    return {"items": [{"id": str(u.id), "username": u.username, "role": u.role} for u in rows]}


@router.patch("/me/prefs")
async def patch_prefs(
    body: dict[str, Any], session: SessionDep, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """Self-service §18.8 overrides of the global §3.7 ambient keys."""
    import re as _re

    _require_on()
    user = await _require_user(session, authorization)
    unknown = set(body) - PREF_KEYS
    if unknown:
        raise HTTPException(
            422, f"unknown pref(s) {sorted(unknown)} — allowed: {sorted(PREF_KEYS)}"
        )
    for key, value in body.items():
        if value is not None and not (
            isinstance(value, list)
            and all(isinstance(v, str) and _re.fullmatch(r"[0-2]\d:[0-5]\d", v) for v in value)
        ):
            raise HTTPException(422, f"{key} must be a list of HH:MM strings (or null to clear)")
    prefs = dict(user.prefs or {})
    for key, value in body.items():
        if value is None:
            prefs.pop(key, None)
        else:
            prefs[key] = value
    user.prefs = prefs
    await session.commit()
    return {"prefs": prefs}
