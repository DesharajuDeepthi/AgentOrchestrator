"""API routers (spec §4) — REST, JSON, mounted at /api/v1."""

from fastapi import APIRouter

from app.api import (
    ambient,
    auth,
    cache,
    chat,
    evals,
    fake_llm,
    mcp_servers,
    memories,
    remote_agents,
    routines,
    runs,
    seed,
    settings,
    skills,
    sub_agents,
    tools,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(cache.router)
api_router.include_router(mcp_servers.router)
api_router.include_router(remote_agents.router)
api_router.include_router(tools.router)
api_router.include_router(skills.router)
api_router.include_router(sub_agents.router)
api_router.include_router(chat.router)
api_router.include_router(runs.router)
api_router.include_router(evals.router)
api_router.include_router(memories.router)
api_router.include_router(routines.router)
api_router.include_router(routines.presence_router)
api_router.include_router(ambient.deliveries_router)
api_router.include_router(ambient.watches_router)
api_router.include_router(ambient.ledger_router)
api_router.include_router(settings.router)
api_router.include_router(seed.router)
api_router.include_router(fake_llm.router)
