"""Scripted A2A counterparty for contract tests (spec §19.7).

Built from the SDK's server half — the §11 fake-provider discipline
applied to A2A. In-process uvicorn on a free port; behavior is keyed on
the message text; auth is enforced by a configurable ASGI wrapper that
also records what credentials arrived (for placement assertions); the
served card is produced live by ``card_modifier`` so skill drift needs
no restart.

Message-text script:
- anything             -> completed, artifact text ``stub-echo: <text>``
- ``ask:<question>``   -> input-required carrying <question>; the reply
                          message completes with ``stub-answered: <reply>``
- ``slow:<seconds>``   -> working for that long, then completed
- ``fail:<reason>``    -> failed with <reason>
- ``reject``           -> rejected
"""

import asyncio
import base64
import binascii
import contextlib
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Part,
    TaskState,
    TextPart,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

OAUTH_TOKEN = "stub-oauth-access-token"  # noqa: S105 - test fixture value
PUBLIC_PATHS = ("/.well-known/agent-card.json", "/token")


def _text_of(context: RequestContext) -> str:
    return context.get_user_input()


class ScriptedExecutor(AgentExecutor):
    def __init__(self, stub: "StubA2AServer") -> None:
        self.stub = stub

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        text = _text_of(context)
        task = context.current_task
        resuming = task is not None and task.status.state == TaskState.input_required
        if task is None:
            await updater.submit()
        await updater.start_work()
        if resuming:
            await updater.add_artifact([Part(root=TextPart(text=f"stub-answered: {text}"))])
            await updater.complete()
            return
        # forced mode (acceptance runner control): behavior set on the STUB, so
        # a natural chat prompt exercises ask/slow paths without magic prefixes
        mode = self.stub.forced_mode
        if mode is not None:
            kind = str(mode.get("kind"))
            if kind == "ask":
                await updater.requires_input(
                    updater.new_agent_message(
                        [Part(root=TextPart(text=str(mode.get("question") or "proceed?")))]
                    )
                )
                return
            if kind in ("slow", "slowask"):
                self.stub.slow_started.set()
                await asyncio.sleep(float(mode.get("delay") or 1))
                if kind == "slowask":
                    await updater.requires_input(
                        updater.new_agent_message(
                            [Part(root=TextPart(text=str(mode.get("question") or "proceed?")))]
                        )
                    )
                else:
                    await updater.add_artifact(
                        [Part(root=TextPart(text=f"stub-slow-done: {text}"))]
                    )
                    await updater.complete()
                return
        if text.startswith("ask:"):
            question = text[4:] or "what should I do?"
            await updater.requires_input(
                updater.new_agent_message([Part(root=TextPart(text=question))])
            )
            return
        if text.startswith("slow:"):
            delay = float(text.split(":", 1)[1] or 1)
            self.stub.slow_started.set()
            await asyncio.sleep(delay)
            await updater.add_artifact([Part(root=TextPart(text=f"stub-slow-done: {delay}"))])
            await updater.complete()
            return
        if text.startswith("slowask:"):
            # sleeps, then asks — the parked-then-input-required path (§19.6)
            _, delay_s, question = text.split(":", 2)
            self.stub.slow_started.set()
            await asyncio.sleep(float(delay_s or 1))
            await updater.requires_input(
                updater.new_agent_message([Part(root=TextPart(text=question or "proceed?"))])
            )
            return
        if text.startswith("fail:"):
            await updater.failed(
                updater.new_agent_message([Part(root=TextPart(text=text[5:] or "boom"))])
            )
            return
        if text == "reject":
            await updater.reject()
            return
        await updater.add_artifact([Part(root=TextPart(text=f"stub-echo: {text}"))])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.stub.cancelled_tasks.append(context.task_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


@dataclass
class StubA2AServer:
    """One scripted counterparty; ``auth`` picks the enforced scheme."""

    name: str = "stub-agent"
    # None | 'apikey-header' | 'apikey-query' | 'apikey-cookie' | 'basic'
    # | 'bearer' | 'oauth2' | 'mtls-only' (declares an unsupported scheme)
    auth: str | None = None
    # acceptance runs bind 0.0.0.0 and advertise the docker-bridge IP so the
    # backend CONTAINER can reach a host-process counterparty; tests keep
    # loopback defaults
    bind_host: str = "127.0.0.1"
    advertise_host: str = "127.0.0.1"
    fixed_port: int = 0
    api_key: str = "stub-api-key"
    basic_user: str = "stub-user"
    basic_pass: str = "stub-pass"  # noqa: S105 - test fixture value
    bearer_token: str = "stub-bearer-token"  # noqa: S105 - test fixture value
    client_id: str = "stub-client"
    client_secret: str = "stub-client-secret"  # noqa: S105 - test fixture value
    skills: list[AgentSkill] = field(default_factory=list)
    seen_auth: list[dict[str, Any]] = field(default_factory=list)
    cancelled_tasks: list[str] = field(default_factory=list)
    token_requests: int = 0
    # when set, overrides the message-text script for NEW tasks:
    # {"kind": "ask"|"slow"|"slowask", "question": str, "delay": float}
    forced_mode: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.skills:
            self.skills = [
                AgentSkill(
                    id="research",
                    name="research",
                    description="Research a topic and report findings",
                    tags=["research", "web"],
                ),
                AgentSkill(
                    id="summarize",
                    name="summarize",
                    description="Summarize a document",
                    tags=["text"],
                ),
            ]
        self.slow_started = asyncio.Event()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.port: int = 0

    # ── card ─────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"http://{self.advertise_host}:{self.port}/"

    @property
    def card_url(self) -> str:
        return f"http://{self.advertise_host}:{self.port}/.well-known/agent-card.json"

    def _security(self) -> tuple[dict[str, Any] | None, list[dict[str, list[str]]] | None]:
        match self.auth:
            case None:
                return None, None
            case "apikey-header":
                scheme: dict[str, Any] = {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
            case "apikey-query":
                scheme = {"type": "apiKey", "in": "query", "name": "api_key"}
            case "apikey-cookie":
                scheme = {"type": "apiKey", "in": "cookie", "name": "stub_session"}
            case "basic":
                scheme = {"type": "http", "scheme": "basic"}
            case "bearer":
                scheme = {"type": "http", "scheme": "bearer"}
            case "oauth2":
                scheme = {
                    "type": "oauth2",
                    "flows": {
                        "clientCredentials": {
                            "tokenUrl": f"http://{self.advertise_host}:{self.port}/token",
                            "scopes": {},
                        }
                    },
                }
            case "mtls-only":
                scheme = {"type": "mutualTLS"}
            case other:  # pragma: no cover - test author error
                raise ValueError(f"unknown auth mode {other!r}")
        return {"main": scheme}, [{"main": []}]

    def make_card(self) -> AgentCard:
        schemes, security = self._security()
        return AgentCard.model_validate(
            {
                "name": self.name,
                "description": "scripted A2A counterparty (spec §19.7)",
                "url": self.url,
                "version": "1.0.0",
                "capabilities": AgentCapabilities(streaming=True).model_dump(exclude_none=True),
                "defaultInputModes": ["text"],
                "defaultOutputModes": ["text"],
                "skills": [s.model_dump(by_alias=True, exclude_none=True) for s in self.skills],
                **({"securitySchemes": schemes, "security": security} if schemes else {}),
            }
        )

    # ── auth enforcement (records + rejects) ─────────────────────

    def _auth_ok(self, request: Request) -> bool:
        seen = {
            "authorization": request.headers.get("Authorization"),
            "x_api_key": request.headers.get("X-Api-Key"),
            "query_api_key": request.query_params.get("api_key"),
            "cookie": request.cookies.get("stub_session"),
        }
        self.seen_auth.append(seen)
        match self.auth:
            case None:
                return True
            case "apikey-header":
                return seen["x_api_key"] == self.api_key
            case "apikey-query":
                return seen["query_api_key"] == self.api_key
            case "apikey-cookie":
                return seen["cookie"] == self.api_key
            case "basic":
                expect = base64.b64encode(f"{self.basic_user}:{self.basic_pass}".encode()).decode()
                return seen["authorization"] == f"Basic {expect}"
            case "bearer":
                return seen["authorization"] == f"Bearer {self.bearer_token}"
            case "oauth2":
                return seen["authorization"] == f"Bearer {OAUTH_TOKEN}"
            case _:
                return False

    async def _token_endpoint(self, request: Request) -> JSONResponse:
        self.token_requests += 1
        form = await request.form()
        client_id = str(form.get("client_id") or "")
        client_secret = str(form.get("client_secret") or "")
        header = request.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            with contextlib.suppress(ValueError, binascii.Error):
                decoded = base64.b64decode(header[6:]).decode()
                client_id, _, client_secret = decoded.partition(":")
        if client_id != self.client_id or client_secret != self.client_secret:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        return JSONResponse(
            {"access_token": OAUTH_TOKEN, "token_type": "Bearer", "expires_in": 3600}
        )

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        # sse-starlette's shutdown watcher has TWO exit sources: the
        # AppStatus.should_exit flag (reset by conftest) and an Issue-132
        # fallback that introspects signal handlers for "the" uvicorn server
        # and honors ITS should_exit. In tests that captured server can be a
        # PREVIOUS stub we already stopped (should_exit=True), which would
        # kill every later SSE stream in this loop — disable the fallback and
        # clear the flag at the moment it matters.
        from sse_starlette.sse import AppStatus

        AppStatus.enable_automatic_graceful_drain = False
        AppStatus.should_exit = False
        handler = DefaultRequestHandler(
            agent_executor=ScriptedExecutor(self), task_store=InMemoryTaskStore()
        )
        app_builder = A2AStarletteApplication(
            agent_card=self.make_card(),
            http_handler=handler,
            card_modifier=lambda _card: self.make_card(),  # live drift (§19.7)
        )
        app = app_builder.build()
        app.router.routes.append(Route("/token", self._token_endpoint, methods=["POST"]))

        stub = self

        class _AuthGate:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                path = scope.get("path", "")
                if (
                    scope["type"] == "http"
                    and path not in PUBLIC_PATHS
                    # runner control endpoints are loopback test plumbing
                    and not path.startswith("/_control/")
                ):
                    request = Request(scope, receive)
                    if not stub._auth_ok(request):
                        response = JSONResponse({"detail": "unauthorized"}, status_code=401)
                        await response(scope, receive, send)
                        return
                await self.inner(scope, receive, send)

        config = uvicorn.Config(
            _AuthGate(app),
            host=self.bind_host,
            port=self.fixed_port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
            await asyncio.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._task, timeout=5)
