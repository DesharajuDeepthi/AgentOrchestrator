"""Standalone runner for the scripted A2A counterparty (spec §19.7, §14d).

The acceptance campaign runs the SAME stub the contract tests use, as a
host process beside the smtp/webhook sinks — no new compose service. The
backend container reaches it over the docker bridge, so the card must
ADVERTISE the bridge IP while uvicorn binds a routable interface:

    .venv/bin/python -m tests.a2a_counterparty --port 8027 \
        --bind-host 0.0.0.0 --advertise-host 172.18.0.1 \
        --name polyglot-agent --auth bearer \
        --skills "translate:Translate text between languages"

Auth modes mirror ``StubA2AServer.auth`` (default: open). Loopback control
endpoints steer live behavior between campaign steps (never the product):

- ``POST /_control/add-skill``   body {id,name,description,tags?} (or empty
  for a default ``translate`` skill) — card drift, served on next fetch
- ``POST /_control/set-mode``    body {kind: ask|slow|slowask, question?,
  delay?} or ``null`` to restore the message-text script — lets a NATURAL
  chat prompt exercise input-required / long-running paths
- ``GET  /_control/state``       {skills, mode, cancelled_tasks, port}
"""

import argparse
import asyncio
import json
from typing import Any

from a2a.types import AgentSkill
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.stub_a2a_server import StubA2AServer


def parse_skills(spec: str | None) -> list[AgentSkill]:
    """``id:name:description:tag,tag;id2:...`` — name/desc/tags optional."""
    out: list[AgentSkill] = []
    for part in (spec or "").split(";"):
        if not part.strip():
            continue
        bits = part.split(":")
        sid = bits[0].strip()
        out.append(
            AgentSkill(
                id=sid,
                name=(bits[1].strip() if len(bits) > 1 and bits[1].strip() else sid),
                description=(bits[2].strip() if len(bits) > 2 else sid),
                tags=[t for t in (bits[3].split(",") if len(bits) > 3 else []) if t],
            )
        )
    return out


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--bind-host", default="0.0.0.0")  # noqa: S104 - sandbox test process
    parser.add_argument("--advertise-host", default="172.18.0.1")
    parser.add_argument("--auth", default=None, help="StubA2AServer auth mode (default open)")
    parser.add_argument("--name", default="stub-agent")
    parser.add_argument("--skills", default=None, help="id:name:desc:tags;... (default card)")
    args = parser.parse_args()

    stub = StubA2AServer(
        name=args.name,
        auth=args.auth,
        bind_host=args.bind_host,
        advertise_host=args.advertise_host,
        fixed_port=args.port,
        skills=parse_skills(args.skills),
    )
    await stub.start()

    async def add_skill(request: Request) -> JSONResponse:
        try:
            body: dict[str, Any] = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        skill = AgentSkill(
            id=str(body.get("id") or "translate"),
            name=str(body.get("name") or body.get("id") or "translate"),
            description=str(body.get("description") or "Translate text between languages"),
            tags=[str(t) for t in (body.get("tags") or [])],
        )
        if all(s.id != skill.id for s in stub.skills):
            stub.skills.append(skill)
        return JSONResponse({"skills": [s.id for s in stub.skills]})

    async def set_mode(request: Request) -> JSONResponse:
        raw = await request.body()
        mode = json.loads(raw) if raw else None
        stub.forced_mode = mode if isinstance(mode, dict) else None
        return JSONResponse({"mode": stub.forced_mode})

    async def state(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "skills": [s.id for s in stub.skills],
                "mode": stub.forced_mode,
                "cancelled_tasks": stub.cancelled_tasks,
                "token_requests": stub.token_requests,
                "port": stub.port,
            }
        )

    assert stub._server is not None  # noqa: S101, SLF001 - test asset introspects its own stub
    stub._server.config.app.inner.router.routes.extend(  # noqa: SLF001
        [
            Route("/_control/add-skill", add_skill, methods=["POST"]),
            Route("/_control/set-mode", set_mode, methods=["POST"]),
            Route("/_control/state", state, methods=["GET"]),
        ]
    )

    print(  # noqa: T201 - runner status line
        f"a2a counterparty '{args.name}' auth={args.auth or 'open'} "
        f"skills={[s.id for s in stub.skills]} "
        f"card={stub.card_url} (bound {args.bind_host}:{stub.port})",
        flush=True,
    )
    try:
        await asyncio.Event().wait()  # serve until killed
    finally:
        await stub.stop()


if __name__ == "__main__":
    asyncio.run(_main())
