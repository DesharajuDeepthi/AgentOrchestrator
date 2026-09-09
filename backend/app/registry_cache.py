"""Registry cache layer (spec §7.3).

Every registry and settings read in the run path goes through the singleton
``RegistryCache``. Its storage backend is selected live by the
``registry_cache_mode`` setting:

- ``bypass`` (default) — stateless: every read executes the same Postgres
  queries the consumers ran before this layer existed.
- ``memory`` — per-process store with per-registry generation counters and
  reload-on-dirty: ``invalidate()`` marks a registry stale, the next read
  reloads it wholesale (registries are small; full reload can never leave a
  stale embedded relationship).
- ``redis`` — the same contract over Redis blobs (read-through, delete on
  invalidate) for future multi-replica deployments. ``REDIS_URL`` env-only.

Freshness contract: **event-invalidated** — every registry write path calls
``invalidate(registry)`` before returning, so visibility stays "next model
call". TTLs are deliberately absent: an entry is current or invalidated.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select

from app.models import Skill, SubAgent, Tool

logger = structlog.get_logger("registry_cache")

Registry = Literal["tools", "skills", "sub_agents", "settings"]
REGISTRIES: tuple[Registry, ...] = ("tools", "skills", "sub_agents", "settings")

_REDIS_PREFIX = "concierge:cache:"
_NOTIFY_CHANNEL = "registry_cache_inv"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _tool_record(t: Tool) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "description": t.description,
        "source": t.source,
        "status": t.status,
        "kind": t.kind,
        "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
        "remote_agent_id": str(t.remote_agent_id) if t.remote_agent_id else None,
        "tool_name": t.tool_name,
        "native_ref": t.native_ref,
        "tool_key": t.tool_key,
        "direct_exposure": t.direct_exposure,
        "input_schema": t.input_schema,
        "embedding": getattr(t, "embedding", None),
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


def _skill_record(s: Skill) -> dict[str, Any]:
    """Snapshot-shaped (superset of factory.snapshot_skill) + registry fields."""
    return {
        "id": str(s.id),
        "name": s.name,
        "description": s.description,
        "source": s.source,
        "status": s.status,
        "kind": s.kind,
        "persona": s.persona,
        "instructions": s.instructions,
        "model": s.model,
        "model_params": s.model_params,
        "direct_exposure": s.direct_exposure,
        "max_tool_iterations": s.max_tool_iterations,
        "embedding": getattr(s, "embedding", None),
        "created_at": _iso(s.created_at),
        "updated_at": _iso(s.updated_at),
        "tools": [
            {
                "id": str(t.id),
                "kind": t.kind,
                "tool_name": t.tool_name,
                "tool_key": t.tool_key,
                "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
                "remote_agent_id": str(t.remote_agent_id) if t.remote_agent_id else None,
                "native_ref": t.native_ref,
                "status": t.status,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in s.tools
            if t.deleted_at is None
        ],
    }


def _sub_agent_record(a: SubAgent) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "description": a.description,
        "source": a.source,
        "status": a.status,
        "kind": a.kind,
        "persona": a.persona,
        "model": a.model,
        "model_params": a.model_params,
        "workflow": a.workflow,
        "native_ref": a.native_ref,
        "direct_exposure": a.direct_exposure,
        "covers_skill_ids": [str(x) for x in (a.covers_skill_ids or [])],
        "skill_ids": [str(s.id) for s in a.skills],
        "skill_names": [s.name for s in a.skills],
        "embedding": getattr(a, "embedding", None),
        "created_at": _iso(a.created_at),
        "updated_at": _iso(a.updated_at),
    }


async def _load_registry(registry: Registry) -> list[dict[str, Any]] | dict[str, Any]:
    """One full non-deleted read of a registry from Postgres (all statuses —
    consumers filter, mirroring the pre-cache per-consumer SQL semantics)."""
    from app.db import get_session_factory
    from app.settings_store import get_settings

    async with get_session_factory()() as session:
        if registry == "tools":
            rows = (
                (
                    await session.execute(
                        select(Tool).where(Tool.deleted_at.is_(None)).order_by(Tool.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_tool_record(t) for t in rows]
        if registry == "skills":
            rows2 = (
                (
                    await session.execute(
                        select(Skill).where(Skill.deleted_at.is_(None)).order_by(Skill.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_skill_record(s) for s in rows2]
        if registry == "sub_agents":
            rows3 = (
                (
                    await session.execute(
                        select(SubAgent)
                        .where(SubAgent.deleted_at.is_(None))
                        .order_by(SubAgent.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_sub_agent_record(a) for a in rows3]
        return await get_settings(session)


class RegistryCache:
    """Mode-dispatching cache facade (spec §7.3). Singleton via get_cache()."""

    def __init__(self) -> None:
        self._mode: str = "bypass"
        self._data: dict[str, list[dict[str, Any]] | dict[str, Any]] = {}
        self._generation: dict[str, int] = dict.fromkeys(REGISTRIES, 0)
        self._loaded_at: dict[str, str | None] = dict.fromkeys(REGISTRIES)
        self._dirty: set[str] = set(REGISTRIES)
        self._locks: dict[str, asyncio.Lock] = {r: asyncio.Lock() for r in REGISTRIES}
        self._redis: Any = None
        # cross-replica sync (spec §7.3): origin id filters own notifications
        self._origin = uuid4().hex
        self._listener_conn: Any = None
        self._listener_tasks: set[Any] = set()

    # ── lifecycle ────────────────────────────────────────────────

    async def startup(self) -> None:
        """Read the mode from settings and warm-load if stateful."""
        self._mode = str(await _load_setting_direct("registry_cache_mode"))
        if self._mode != "bypass":
            for registry in REGISTRIES:
                try:
                    await self._ensure(registry)
                except Exception as exc:  # noqa: BLE001 — warm load is best-effort
                    logger.warning("cache_warm_load_failed", registry=registry, error=str(exc))
        await self.start_listener()
        logger.info("registry_cache_started", mode=self._mode)

    async def set_mode(self, mode: str) -> None:
        """Live mode flip (called by the settings write path)."""
        if mode == self._mode:
            return
        self._mode = mode
        self._dirty = set(REGISTRIES)
        if mode == "memory":
            for registry in REGISTRIES:
                await self._ensure(registry)
        logger.info("registry_cache_mode_changed", mode=mode)

    # ── invalidation / refresh / status ──────────────────────────

    async def _mark_dirty(self, registry: Registry) -> None:
        """Local invalidation only — no cross-replica notify (the LISTEN
        callback uses this, which is what makes notification loops
        impossible by construction)."""
        # relationship propagation: skills embed tool rows, sub_agent records
        # embed skill names — dirtying the parent dirties the dependents, so
        # no write hook needs to know the dependency graph
        dependents: dict[str, tuple[Registry, ...]] = {
            "tools": ("skills",),
            "skills": ("sub_agents",),
        }
        queue: list[Registry] = [registry]
        seen: set[str] = set()
        while queue:
            reg = queue.pop()
            if reg in seen:
                continue
            seen.add(reg)
            self._generation[reg] += 1
            self._dirty.add(reg)
            if self._mode == "redis":
                try:
                    redis = await self._get_redis()
                    await redis.delete(f"{_REDIS_PREFIX}{reg}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cache_redis_invalidate_failed", registry=reg, error=str(exc))
            queue.extend(dependents.get(reg, ()))
        if registry == "settings":
            # the mode itself lives in settings — re-read it so a PATCH flips live
            mode = str(await _load_setting_direct("registry_cache_mode"))
            if mode != self._mode:
                await self.set_mode(mode)

    async def invalidate(self, registry: Registry) -> None:
        await self._mark_dirty(registry)
        await self._notify_peers(registry)

    # ── cross-replica sync (spec §7.3: LISTEN/NOTIFY, dormant single-node) ──

    async def _notify_peers(self, registry: Registry) -> None:
        """Broadcast the invalidation so other replicas mark their local
        caches dirty. Best-effort: single-replica correctness never depends
        on it (this process already marked itself dirty)."""
        from app.db import get_session_factory

        try:
            from sqlalchemy import text

            async with get_session_factory()() as session:
                await session.execute(
                    text("SELECT pg_notify(:ch, :payload)"),
                    {"ch": _NOTIFY_CHANNEL, "payload": f"{self._origin}:{registry}"},
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_notify_failed", registry=registry, error=str(exc))

    async def start_listener(self) -> None:
        """LISTEN for peer invalidations on a dedicated connection. Safe to
        call when already listening; failure logs and leaves single-node
        behavior untouched."""
        if self._listener_conn is not None:
            return
        try:
            import asyncpg  # type: ignore[import-untyped]

            from app.config import get_config

            dsn = get_config().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
            conn = await asyncpg.connect(dsn)

            def _on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
                origin, _, registry = payload.partition(":")
                if origin == self._origin or registry not in REGISTRIES:
                    return
                task = asyncio.create_task(self._mark_dirty(registry))
                self._listener_tasks.add(task)
                task.add_done_callback(self._listener_tasks.discard)

            await conn.add_listener(_NOTIFY_CHANNEL, _on_notify)
            self._listener_conn = conn
            logger.info("cache_listener_started", channel=_NOTIFY_CHANNEL, origin=self._origin)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_listener_unavailable", error=str(exc))

    async def stop_listener(self) -> None:
        if self._listener_conn is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await self._listener_conn.close()
            self._listener_conn = None

    async def refresh(self, registry: Registry) -> dict[str, Any]:
        """Operator-forced eager reload (§8.7 buttons). In bypass, just counts."""
        await self.invalidate(registry)
        if self._mode == "bypass":
            data = await _load_registry(registry)
        else:
            data = await self._ensure(registry, force=True)
        return self._status_entry(registry, data)

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self._mode, "registries": {}}
        for registry in REGISTRIES:
            data = self._data.get(registry)
            if self._mode == "bypass" or data is None:
                out["registries"][registry] = {
                    "records": None,
                    "generation": self._generation[registry],
                    "loaded_at": None,
                    "cached": False,
                }
            else:
                out["registries"][registry] = self._status_entry(registry, data)
        return out

    def _status_entry(
        self, registry: Registry, data: list[dict[str, Any]] | dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "records": len(data),
            "generation": self._generation[registry],
            "loaded_at": self._loaded_at.get(registry),
            "cached": self._mode != "bypass",
        }

    # ── storage ──────────────────────────────────────────────────

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            from app.config import get_config

            url = get_config().redis_url
            if not url:
                raise RuntimeError("registry_cache_mode 'redis' requires REDIS_URL")
            self._redis = aioredis.from_url(url, decode_responses=True)
        return self._redis

    async def _ensure(
        self, registry: Registry, *, force: bool = False
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return current data for a registry under the active mode."""
        if self._mode == "bypass":
            return await _load_registry(registry)
        async with self._locks[registry]:
            if self._mode == "redis":
                redis = await self._get_redis()
                key = f"{_REDIS_PREFIX}{registry}"
                if not force:
                    blob = await redis.get(key)
                    if blob is not None:
                        loaded: list[dict[str, Any]] | dict[str, Any] = json.loads(blob)
                        return loaded
                data = await _load_registry(registry)
                await redis.set(key, json.dumps(data))
                self._data[registry] = data
                self._loaded_at[registry] = datetime.now(UTC).isoformat()
                self._dirty.discard(registry)
                return data
            # memory: reload-on-dirty
            if force or registry in self._dirty or registry not in self._data:
                data = await _load_registry(registry)
                self._data[registry] = data
                self._loaded_at[registry] = datetime.now(UTC).isoformat()
                self._dirty.discard(registry)
            return self._data[registry]

    # ── typed reads (consumers) ──────────────────────────────────

    async def tools(self, *, exposed_only: bool) -> list[dict[str, Any]]:
        rows = await self._ensure("tools")
        assert isinstance(rows, list)
        return [
            t
            for t in rows
            if t["status"] == "active" and (t["direct_exposure"] or not exposed_only)
        ]

    async def tools_by_ids(self, ids: list[UUID | str]) -> list[dict[str, Any]]:
        """Order-preserving, active-only — mirrors factory.resolve_tools_by_ids."""
        rows = await self._ensure("tools")
        assert isinstance(rows, list)
        by_id = {t["id"]: t for t in rows if t["status"] == "active"}
        return [by_id[str(i)] for i in ids if str(i) in by_id]

    async def tool_by_id(self, tool_id: UUID | str) -> dict[str, Any] | None:
        rows = await self._ensure("tools")
        assert isinstance(rows, list)
        return next((t for t in rows if t["id"] == str(tool_id)), None)

    async def skills(self, *, exposed_only: bool) -> list[dict[str, Any]]:
        rows = await self._ensure("skills")
        assert isinstance(rows, list)
        return [
            s
            for s in rows
            if s["status"] == "active" and (s["direct_exposure"] or not exposed_only)
        ]

    async def skill_by_id(self, skill_id: UUID | str) -> dict[str, Any] | None:
        rows = await self._ensure("skills")
        assert isinstance(rows, list)
        return next((s for s in rows if s["id"] == str(skill_id)), None)

    async def sub_agents(self) -> list[dict[str, Any]]:
        """Active only, created_at order (rung-3 precedence relies on it)."""
        rows = await self._ensure("sub_agents")
        assert isinstance(rows, list)
        return [a for a in rows if a["status"] == "active"]

    async def sub_agent_by_id(self, agent_id: UUID | str) -> dict[str, Any] | None:
        rows = await self._ensure("sub_agents")
        assert isinstance(rows, list)
        return next((a for a in rows if a["id"] == str(agent_id)), None)

    async def sub_agent_cards(self) -> list[dict[str, Any]]:
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "kind": a["kind"],
                "description": a["description"],
                "skills": a["skill_names"],
            }
            for a in await self.sub_agents()
        ]

    async def sub_agent_snapshot(self, agent_id: UUID | str) -> dict[str, Any] | None:
        """Assembled like factory.snapshot_sub_agent, from cached registries."""
        agent = await self.sub_agent_by_id(agent_id)
        if agent is None:
            return None
        workflow = agent["workflow"] or {}
        skill_ids = [
            n["skill_id"]
            for n in workflow.get("nodes", [])
            if n.get("type") == "skill" and n.get("skill_id")
        ]
        skills: dict[str, dict[str, Any]] = {}
        for sid in skill_ids:
            snap = await self.skill_by_id(sid)
            if snap is not None:
                skills[str(sid)] = snap
        return {
            "sub_agent": {
                "id": agent["id"],
                "name": agent["name"],
                "persona": agent["persona"],
                "model": agent["model"],
                "model_params": agent["model_params"],
                "kind": agent["kind"],
                "source": agent["source"],
                "updated_at": agent["updated_at"],
            },
            "workflow": workflow,
            "skills": skills,
        }

    async def setting(self, key: str) -> Any:
        data = await self._ensure("settings")
        assert isinstance(data, dict)
        return data[key]


_CACHE: RegistryCache | None = None


def get_cache() -> RegistryCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = RegistryCache()
    return _CACHE


def reset_cache() -> None:
    """Test hook: drop the singleton (fresh mode read on next use)."""
    global _CACHE
    _CACHE = None


async def _load_setting_direct(key: str) -> Any:
    """Read one setting straight from Postgres — used to learn the cache's own
    mode without consulting the cache (avoids the bootstrap cycle)."""
    from app.db import get_session_factory
    from app.settings_store import get_setting

    async with get_session_factory()() as session:
        return await get_setting(session, key)
