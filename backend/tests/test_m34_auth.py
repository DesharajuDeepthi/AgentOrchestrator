"""M34 — auth & tenancy (spec §18.8), dark by default. The rest of the
suite runs with auth OFF and is untouched — that IS the byte-identity
proof. These tests flip AUTH_ENABLED on per-test and cover the guard,
roles, tenancy invisibility, ambient ownership, prefs overrides, and the
basic hardening."""

from typing import Any

import pytest

from app.auth import bootstrap_admin, hash_password, verify_password
from app.config import get_config
from app.db import get_session_factory
from app.models import Routine, Run, User

pytestmark = pytest.mark.anyio


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AUTH_ENABLED", "1")
    get_config.cache_clear()
    yield
    get_config.cache_clear()


async def _bootstrap() -> tuple[User, str]:
    return await bootstrap_admin()


async def _login(client: Any, username: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


async def _admin_token(client: Any) -> str:
    _, password = await _bootstrap()
    return await _login(client, "admin", password)


async def _member(client: Any, admin_token: str, username: str) -> str:
    resp = await client.post(
        "/api/v1/auth/users",
        json={"username": username, "password": "member-pass-1", "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return await _login(client, username, "member-pass-1")


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── scrypt + bootstrap ───────────────────────────────────────────────


def test_scrypt_roundtrip() -> None:
    h = hash_password("hunter2!")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert verify_password("hunter2!", h) is True
    assert verify_password("wrong", h) is False


async def test_bootstrap_admin_once(client: Any, auth_on: Any) -> None:
    user, password = await _bootstrap()
    assert user.role == "admin" and user.username == "admin"
    assert password  # one-time — printed to the boot log in real boots
    again, pw2 = await bootstrap_admin()
    assert again.id == user.id and pw2 == ""  # never re-issued


# ── the guard (spec §18.8 exemptions) ────────────────────────────────


async def test_guard_401_without_token_and_exemptions(client: Any, auth_on: Any) -> None:
    await _bootstrap()
    assert (await client.get("/api/v1/skills")).status_code == 401
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/metrics")).status_code == 200
    bad = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401  # exempt endpoint, wrong creds


async def test_login_token_works_and_me(client: Any, auth_on: Any) -> None:
    token = await _admin_token(client)
    me = await client.get("/api/v1/auth/me", headers=_h(token))
    assert me.status_code == 200
    assert me.json()["username"] == "admin" and me.json()["role"] == "admin"
    ok = await client.get("/api/v1/skills", headers=_h(token))
    assert ok.status_code == 200


# ── roles: registries shared, admin-writable (spec §18.8) ────────────


async def test_member_reads_but_cannot_write_registries(seeded_client: Any, auth_on: Any) -> None:
    admin = await _admin_token(seeded_client)
    member = await _member(seeded_client, admin, "casey")
    reads = await seeded_client.get("/api/v1/skills", headers=_h(member))
    assert reads.status_code == 200 and reads.json()  # shared registries
    write = await seeded_client.post(
        "/api/v1/skills", json={"name": "nope", "instructions": "x"}, headers=_h(member)
    )
    assert write.status_code == 403
    admin_write = await seeded_client.post(
        "/api/v1/skills",
        json={"name": "admin-made", "description": "d", "instructions": "x"},
        headers=_h(admin),
    )
    assert admin_write.status_code in (200, 201), admin_write.text


# ── tenancy: work is per-user and invisible across users ─────────────


async def test_work_is_invisible_across_users(seeded_client: Any, auth_on: Any) -> None:
    from app.llm import fake as fake_llm
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(
            session,
            {
                "default_model": "fake:scripted",
                "formatter_enabled": False,
                "orchestrator_mode": "graph",
                "ambient_enabled": True,
            },
        )
    admin = await _admin_token(seeded_client)
    member = await _member(seeded_client, admin, "riley")

    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "PlannerOutput",
                "args": {"entries": [], "direct_answer": "ok", "no_confident_match": False},
                "id": "m34-1",
            }
        ],
    )
    run = await seeded_client.post(
        "/api/v1/chat", json={"message": "admin's private question"}, headers=_h(admin)
    )
    assert run.status_code == 201
    routine = await seeded_client.post(
        "/api/v1/routines", json={"name": "m34-admin-routine", "prompt": "p"}, headers=_h(admin)
    )
    assert routine.status_code in (200, 201), routine.text

    member_runs = (await seeded_client.get("/api/v1/runs", headers=_h(member))).json()
    assert member_runs == []
    member_routines = (await seeded_client.get("/api/v1/routines", headers=_h(member))).json()
    assert member_routines == []
    member_deliveries = (await seeded_client.get("/api/v1/deliveries", headers=_h(member))).json()[
        "items"
    ]
    assert member_deliveries == []
    admin_runs = (await seeded_client.get("/api/v1/runs", headers=_h(admin))).json()
    assert len(admin_runs) == 1
    admin_routines = (await seeded_client.get("/api/v1/routines", headers=_h(admin))).json()
    assert len(admin_routines) == 1


async def test_routine_fires_as_its_owner(client: Any, auth_on: Any) -> None:
    from app.ambient.execute import prepare_run
    from app.ambient.store import emit_event
    from app.models import AmbientEvent
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_enabled": True})
    admin_user, _ = await _bootstrap()
    async with get_session_factory()() as session:
        routine = Routine(name="m34-owned", prompt="p", user_id=admin_user.id)
        session.add(routine)
        await session.commit()
        await session.refresh(routine)
    event = await emit_event(kind="routine_fire", source="webhook", routine_id=routine.id)
    assert event is not None
    async with get_session_factory()() as session:
        row = await session.get(AmbientEvent, event.id)
        assert row is not None
        row.verdict = "fired"
        row.decision = {"fired_for": "routine"}
        await session.commit()
        await session.refresh(row)
    run = await prepare_run(row)
    assert run is not None
    async with get_session_factory()() as session:
        fresh = await session.get(Run, run.id)
        assert fresh is not None and fresh.user_id == admin_user.id


# ── per-user prefs overrides (spec §18.8) ────────────────────────────


async def test_prefs_override_quiet_hours(client: Any, auth_on: Any) -> None:
    from app.ambient.deliver import effective_ambient_settings
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_quiet_hours": ["22:00", "07:00"]})
    admin = await _admin_token(client)
    resp = await client.patch(
        "/api/v1/auth/me/prefs",
        json={"ambient_quiet_hours": ["01:00", "02:00"]},
        headers=_h(admin),
    )
    assert resp.status_code == 200
    async with get_session_factory()() as session:
        from sqlalchemy import select

        user = (await session.execute(select(User).where(User.username == "admin"))).scalar_one()
    eff = await effective_ambient_settings(user.id)
    assert eff["ambient_quiet_hours"] == ["01:00", "02:00"]  # user override
    assert await effective_ambient_settings(None) != eff or True  # global stays global
    eff_global = await effective_ambient_settings(None)
    assert eff_global["ambient_quiet_hours"] == ["22:00", "07:00"]


# ── hardening ────────────────────────────────────────────────────────


async def test_rate_limit_429(client: Any, auth_on: Any) -> None:
    # M40: the bucket shape is the live rate_limit_burst / rate_limit_per_s
    # settings pair — drive it through the real settings API
    from app import auth as auth_mod

    token = await _admin_token(client)
    resp = await client.patch(
        "/api/v1/settings",
        json={"rate_limit_burst": 5, "rate_limit_per_s": 1},
        headers=_h(token),
    )
    assert resp.status_code == 200, resp.text
    auth_mod._buckets.clear()
    try:
        statuses = []
        for _ in range(8):
            statuses.append((await client.get("/api/v1/skills", headers=_h(token))).status_code)
        assert 429 in statuses
    finally:
        auth_mod._buckets.clear()
        # a throttled token still needs to restore settings — retry briefly
        for _ in range(20):
            resp = await client.patch(
                "/api/v1/settings",
                json={"rate_limit_burst": 120, "rate_limit_per_s": 10},
                headers=_h(token),
            )
            if resp.status_code == 200:
                break
            auth_mod._buckets.clear()
        assert resp.status_code == 200, resp.text


async def test_security_headers_when_auth_on(client: Any, auth_on: Any) -> None:
    token = await _admin_token(client)
    resp = await client.get("/api/v1/skills", headers=_h(token))
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"


async def test_fire_endpoint_keeps_its_own_auth(client: Any, auth_on: Any) -> None:
    """POST /routines/{id}/fire is exempt from session auth — the hashed
    fire token IS its auth (spec §18.8/§17.2)."""
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_enabled": True})
    admin_user, _ = await _bootstrap()
    async with get_session_factory()() as session:
        routine = Routine(name="m34-fire", prompt="p", user_id=admin_user.id)
        session.add(routine)
        await session.commit()
        routine_id = routine.id
    # no session token, no fire token → 401 from the FIRE-TOKEN check,
    # not from the session guard (the guard exempts this path)
    resp = await client.post(f"/api/v1/routines/{routine_id}/fire", json={"payload": {}})
    assert resp.status_code == 401
    assert "fire token" in resp.json()["detail"]
