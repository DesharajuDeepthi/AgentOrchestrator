"""A2A remote-agent manager (spec §19.2/§19.4).

Singleton peer of the MCP manager, simpler by design: A2A is stateless
HTTP, so there are no persistent sessions to hold — per-agent state is
the last fetched Agent Card. Responsibilities: fetch + validate cards,
project ``auth_schemes`` for the UI, ingest card skills into the tools
registry (``kind='a2a'``, MCP ingest semantics: refresh-in-place,
inactive on vanish, collision-suffixed ``tool_key``, cache invalidation),
run the card-refresh loop (interval + master switch re-read live each
cycle), and build authenticated SDK clients for the call path.

The ``a2a`` / ``authlib`` SDKs are imported only inside ``app/a2a/``
(spec §19.1 — the §2.1 isolation discipline applied to A2A).
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import structlog
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import AgentCard
from sqlalchemy import select

from app.a2a.auth import AgentCredentialService, ConciergeAuthInterceptor, scheme_supported
from app.db import get_session_factory
from app.models import RemoteAgent, Tool

logger = structlog.get_logger("a2a")

CARD_FETCH_TIMEOUT_S = 15.0
DEFAULT_CARD_PATH = "/.well-known/agent-card.json"
_DARK_SLEEP_S = 30.0

# the fixed invocation surface of every projected a2a tool (spec §19.4):
# A2A skills are advisory — invocation is agent-level message/send
A2A_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The task or question to send to the remote agent, in plain language.",
        },
        "data": {
            "type": "object",
            "description": "Optional structured data to attach to the message.",
        },
    },
    "required": ["message"],
}


def split_card_url(card_url: str) -> tuple[str, str]:
    """A registered URL may be a base URL or the full card path."""
    parts = urlsplit(card_url)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or ""
    if path.endswith(".json"):
        return base, path
    return f"{base}{path.rstrip('/')}", DEFAULT_CARD_PATH


def project_auth_schemes(card: AgentCard) -> dict[str, Any]:
    """UI projection of the card's securitySchemes (spec §19.3)."""
    out: dict[str, Any] = {}
    for name, wrapper in (card.security_schemes or {}).items():
        scheme_def = wrapper.root
        out[name] = {
            "type": getattr(scheme_def, "type", "unknown"),
            "supported": scheme_supported(scheme_def),
        }
    return out


def skill_description(skill: Any) -> str:
    """Planner routing signal: name + description + tags digest (§19.4)."""
    parts = [f"{skill.name}: {skill.description}".strip().rstrip(":").strip()]
    if skill.tags:
        parts.append(f"[tags: {', '.join(skill.tags)}]")
    return " ".join(p for p in parts if p)


class A2AManager:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=CARD_FETCH_TIMEOUT_S)
        self._refresh_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Refresh every persisted agent (when a2a is on) + refresh loop."""
        # M40: `a2a_http_timeout_s` is applied on the manager's next client
        # build (this start) — __init__ keeps the code default for tests
        # that never start the manager
        try:
            from app.registry_cache import get_cache

            timeout_s = max(float(await get_cache().setting("a2a_http_timeout_s")), 1.0)
        except Exception:  # noqa: BLE001 — settings hiccup keeps the default client
            timeout_s = CARD_FETCH_TIMEOUT_S
        if timeout_s != CARD_FETCH_TIMEOUT_S:
            old = self._http
            self._http = httpx.AsyncClient(timeout=timeout_s)
            await old.aclose()
        if await self._enabled():
            async with get_session_factory()() as db:
                agent_ids = list(
                    (
                        await db.execute(
                            select(RemoteAgent.id).where(RemoteAgent.deleted_at.is_(None))
                        )
                    ).scalars()
                )
            await asyncio.gather(
                *(self.refresh_agent(aid) for aid in agent_ids), return_exceptions=True
            )
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        await self._http.aclose()

    async def _enabled(self) -> bool:
        from app.registry_cache import get_cache

        return bool(await get_cache().setting("a2a_enabled"))

    # ── card fetch + ingest (spec §19.2/§19.4) ───────────────────

    async def fetch_card(self, card_url: str) -> AgentCard:
        base, path = split_card_url(card_url)
        resolver = A2ACardResolver(self._http, base, agent_card_path=path)
        return await resolver.get_agent_card()

    async def refresh_agent(self, agent_id: UUID) -> None:
        """(Re)fetch the card, project schemes, ingest skills, set status."""
        async with get_session_factory()() as db:
            agent = await db.get(RemoteAgent, agent_id)
            if agent is None or agent.deleted_at is not None:
                return
            card_url = agent.card_url
        try:
            card = await self.fetch_card(card_url)
        except BaseException as exc:  # noqa: BLE001 - recorded on the row
            await self._record_status(agent_id, "error", error=_describe(exc))
            logger.warning(
                "a2a_card_fetch_failed",
                tier="a2a",
                kind="card_fetch",
                agent_id=str(agent_id),
                error=_describe(exc),
            )
            return
        async with get_session_factory()() as db:
            agent = await db.get(RemoteAgent, agent_id)
            if agent is None or agent.deleted_at is not None:
                return
            agent.card = card.model_dump(mode="json", by_alias=True, exclude_none=True)
            agent.card_fetched_at = datetime.now(UTC)
            agent.auth_schemes = project_auth_schemes(card)
            agent.status = "active"
            agent.last_error = None
            await db.commit()
        await self._ingest(agent_id, card)
        logger.info(
            "a2a_card_refreshed",
            tier="a2a",
            kind="card_fetch",
            agent_id=str(agent_id),
            skills=len(card.skills),
        )

    async def _ingest(self, agent_id: UUID, card: AgentCard) -> None:
        """Project card skills into the tools registry (MCP _ingest semantics)."""
        async with get_session_factory()() as db:
            agent = await db.get(RemoteAgent, agent_id)
            if agent is None:
                return
            existing = {
                t.tool_name: t
                for t in (
                    await db.execute(select(Tool).where(Tool.remote_agent_id == agent_id))
                ).scalars()
            }
            taken_keys = set((await db.execute(select(Tool.tool_key))).scalars())
            seen: set[str] = set()
            for skill in card.skills:
                seen.add(skill.id)
                row = existing.get(skill.id)
                if row is None:
                    key = f"{agent.name}.{skill.name}"
                    if key in taken_keys:  # collision-safe (spec §3.2)
                        key = f"{key}-{uuid4().hex[:6]}"
                    taken_keys.add(key)
                    db.add(
                        Tool(
                            name=skill.name,
                            description=skill_description(skill),
                            kind="a2a",
                            source=agent.source,  # inherited (spec §19.4)
                            status="active",
                            remote_agent_id=agent_id,
                            tool_name=skill.id,
                            tool_key=key,
                            input_schema=A2A_TOOL_INPUT_SCHEMA,
                        )
                    )
                else:
                    row.description = skill_description(skill)
                    row.input_schema = A2A_TOOL_INPUT_SCHEMA
                    row.status = "active"
                    row.deleted_at = None
            for skill_id, row in existing.items():
                if skill_id not in seen and row.status != "inactive":
                    row.status = "inactive"  # removed skills marked inactive
            await db.commit()
        from app.registry_cache import get_cache

        await get_cache().invalidate("tools")
        logger.info(
            "a2a_skills_ingested",
            tier="a2a",
            kind="ingest",
            agent_id=str(agent_id),
            skill_count=len(card.skills),
        )

    async def _refresh_loop(self) -> None:
        from app.registry_cache import get_cache

        while True:
            try:
                if not await self._enabled():
                    await asyncio.sleep(_DARK_SLEEP_S)
                    continue
                interval = int(await get_cache().setting("a2a_card_refresh_interval_s"))
            except Exception:  # cache not up yet — stay quiet, retry
                await asyncio.sleep(_DARK_SLEEP_S)
                continue
            await asyncio.sleep(max(interval, 5))
            with contextlib.suppress(Exception):
                if not await self._enabled():
                    continue
                async with get_session_factory()() as db:
                    agent_ids = list(
                        (
                            await db.execute(
                                select(RemoteAgent.id).where(RemoteAgent.deleted_at.is_(None))
                            )
                        ).scalars()
                    )
                for aid in agent_ids:
                    await self.refresh_agent(aid)

    # ── call path (spec §19.5 — consumed by the M38 proxy) ───────

    async def build_client(self, agent_id: UUID) -> tuple[Client, AgentCard]:
        """An authenticated SDK client for one registered agent."""
        async with get_session_factory()() as db:
            agent = await db.get(RemoteAgent, agent_id)
            if agent is None or agent.deleted_at is not None or agent.card is None:
                raise RuntimeError(f"remote agent {agent_id} is not registered")
            card = AgentCard.model_validate(agent.card)
            credentials = dict(agent.credentials or {})
        service = AgentCredentialService(agent_id=str(agent_id), card=card, credentials=credentials)
        # streaming preferred; the factory falls back to blocking message/send
        # for cards that don't declare streaming (polling=False — that flag
        # would force blocking sends and starve early task events, §19.6)
        config = ClientConfig(streaming=True, polling=False, httpx_client=self._http)
        client = ClientFactory(config).create(
            card, interceptors=[ConciergeAuthInterceptor(service)]
        )
        return client, card

    # ── helpers ──────────────────────────────────────────────────

    async def _record_status(self, agent_id: UUID, status: str, error: str | None = None) -> None:
        async with get_session_factory()() as db:
            agent = await db.get(RemoteAgent, agent_id)
            if agent is None:
                return
            agent.status = status
            agent.last_error = error
            await db.commit()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "card fetch timed out"
    if isinstance(exc, BaseExceptionGroup):
        parts = [_describe(e) for e in exc.exceptions]
        return "; ".join(dict.fromkeys(parts))
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


_manager: A2AManager | None = None


def get_manager() -> A2AManager | None:
    return _manager


def set_manager(manager: A2AManager | None) -> None:
    global _manager
    _manager = manager
