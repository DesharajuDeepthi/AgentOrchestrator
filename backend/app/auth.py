"""Auth & tenancy core (spec §18.8 — milestone M34, dark by default).

`AUTH_ENABLED` off ⇒ every function here degrades to a no-op and the
platform is byte-identical single-user. On ⇒ scrypt password hashes,
bearer sessions hashed at rest with a TTL, a guard middleware over
/api/v1 (exempt: health, metrics, login, and the routine fire endpoint,
which keeps its own hashed fire-token auth), admin-gated registry writes,
a per-user token-bucket rate limit, and security headers.

The current requester rides a contextvar so stores and the orchestrator
scope work rows without threading a parameter through every call; the run
executor re-binds it from the Run's owner so in-run writes scope to the
owner even off-request (ambient fires, eval runs)."""

import hashlib
import hmac
import os
import re
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.db import get_session_factory
from app.models import AuthSession, User

logger = structlog.get_logger("auth")

# M40: session lifetime is env like the auth master switch; the rate-limit
# shape is a live setting pair (rate_limit_burst / rate_limit_per_s) read
# per request — these module values are the code defaults only
SESSION_TTL_H = int(os.environ.get("AUTH_SESSION_TTL_H", "24") or "24")
RATE_LIMIT_BURST = 120  # default tokens per bucket (rate_limit_burst)
RATE_LIMIT_REFILL_PER_S = 10.0  # default refill (rate_limit_per_s)
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

_current_user: ContextVar[dict[str, Any] | None] = ContextVar("current_user", default=None)
_buckets: dict[str, tuple[float, float]] = {}  # user/ip → (tokens, last_ts)

# guard exemptions (spec §18.8): the fire endpoint keeps its own token auth
_EXEMPT = re.compile(r"^/api/v1/(auth/login$|routines/[0-9a-f-]+/fire$)")
# writes to these resources require the admin role; /invoke and /overlap*
# are member actions (read + invoke), not definition writes
_ADMIN_WRITE = re.compile(r"^/api/v1/(mcp-servers|remote-agents|tools|skills|sub-agents|settings)")


def auth_enabled() -> bool:
    from app.config import get_config

    return bool(get_config().auth_enabled)


def current_user() -> dict[str, Any] | None:
    return _current_user.get()


def current_user_id() -> UUID | None:
    user = _current_user.get()
    return UUID(str(user["id"])) if user else None


def set_current_user(user: dict[str, Any] | None) -> None:
    _current_user.set(user)


def bind_run_owner(user_id: UUID | None) -> None:
    """Run tasks re-bind the requester from the Run's owner (§18.8) so
    ambient fires and eval runs scope their writes to the owner."""
    _current_user.set({"id": str(user_id)} if user_id is not None else None)


# ── scrypt passwords ─────────────────────────────────────────────────


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:  # noqa: BLE001 — malformed hash is a refusal
        return False


# ── bearer sessions (hashed at rest, TTL) ────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(user: User) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=SESSION_TTL_H)
    async with get_session_factory()() as session:
        session.add(AuthSession(user_id=user.id, token_hash=_token_hash(token), expires_at=expires))
        # opportunistic cleanup of expired sessions
        await session.execute(delete(AuthSession).where(AuthSession.expires_at < datetime.now(UTC)))
        await session.commit()
    return token, expires


async def authenticate(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
            )
        ).scalar_one_or_none()
        if row is None or row.expires_at < datetime.now(UTC):
            return None
        user = await session.get(User, row.user_id)
    if user is None:
        return None
    return {"id": str(user.id), "username": user.username, "role": user.role}


async def revoke_session(token: str) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            delete(AuthSession).where(AuthSession.token_hash == _token_hash(token))
        )
        await session.commit()


async def bootstrap_admin() -> tuple[User, str]:
    """First boot with auth on: create 'admin' with a one-time password
    that prints to the boot log. Returns ('' password when already present
    — a password is never re-issued)."""
    async with get_session_factory()() as session:
        existing = (
            await session.execute(select(User).where(User.username == "admin"))
        ).scalar_one_or_none()
        if existing is not None:
            return existing, ""
        password = secrets.token_urlsafe(12)
        user = User(username="admin", password_hash=hash_password(password), role="admin")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    logger.warning(
        "auth_bootstrap_admin",
        username="admin",
        one_time_password=password,
        note="shown ONCE — change it or store it now",
    )
    return user, password


# ── the guard middleware (spec §18.8) ────────────────────────────────


def _rate_limited(
    key: str,
    burst: float = float(RATE_LIMIT_BURST),
    per_s: float = RATE_LIMIT_REFILL_PER_S,
) -> bool:
    now = time.monotonic()
    tokens, last = _buckets.get(key, (burst, now))
    tokens = min(burst, tokens + (now - last) * per_s)
    if tokens < 1.0:
        _buckets[key] = (tokens, now)
        return True
    _buckets[key] = (tokens - 1.0, now)
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not auth_enabled():
            set_current_user(None)  # byte-identity: no checks, no headers
            passthrough: Response = await call_next(request)
            return passthrough
        path = request.url.path
        if not path.startswith("/api/v1") or _EXEMPT.match(path):
            set_current_user(None)
            response: Response = await call_next(request)
            return _harden(response)
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not token:
            # EventSource cannot set headers — SSE endpoints accept the
            # session token as a query param (still hashed-at-rest, TTL'd)
            token = str(request.query_params.get("token") or "")
        user = await authenticate(token)
        if user is None:
            return _harden(JSONResponse({"detail": "authentication required"}, status_code=401))
        from app.registry_cache import get_cache

        try:  # M40: live rate-limit shape; a settings hiccup falls back to defaults
            burst = float(await get_cache().setting("rate_limit_burst"))
            per_s = float(await get_cache().setting("rate_limit_per_s"))
        except Exception:  # noqa: BLE001
            burst, per_s = float(RATE_LIMIT_BURST), RATE_LIMIT_REFILL_PER_S
        if _rate_limited(user["id"], burst, per_s):
            return _harden(JSONResponse({"detail": "rate limit exceeded"}, status_code=429))
        if (
            request.method in {"POST", "PATCH", "PUT", "DELETE"}
            and _ADMIN_WRITE.match(path)
            and "/invoke" not in path
            and "/overlap" not in path
            and user["role"] != "admin"
        ):
            return _harden(
                JSONResponse(
                    {"detail": "registry and settings writes require the admin role"},
                    status_code=403,
                )
            )
        set_current_user(user)
        request.state.user = user
        try:
            response = await call_next(request)
        finally:
            set_current_user(None)
        return _harden(response)


def _harden(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def scope_to_user(stmt: Any, model: Any) -> Any:
    """§18.8 tenancy filter: per-user work queries see only the requester's
    rows when auth is on; unchanged (single-user) when dark."""
    if not auth_enabled():
        return stmt
    return stmt.where(model.user_id == current_user_id())


def owns_row(row: Any) -> bool:
    """True when the requester may see this work row (§18.8)."""
    if not auth_enabled():
        return True
    owner = getattr(row, "user_id", None)
    return owner == current_user_id()
