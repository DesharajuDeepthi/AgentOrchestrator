"""Registry cache status + operator refresh endpoints (spec §7.3/§8.7)."""

from typing import Any

from fastapi import APIRouter, HTTPException

from app.registry_cache import REGISTRIES, get_cache

router = APIRouter(prefix="/cache", tags=["cache"])


@router.get("/status")
async def cache_status() -> dict[str, Any]:
    return await get_cache().status()


@router.post("/refresh/{registry}")
async def refresh_registry(registry: str) -> dict[str, Any]:
    """Force an eager reload of one registry — or 'all' (§8.7 buttons)."""
    cache = get_cache()
    if registry == "all":
        out: dict[str, Any] = {}
        for r in REGISTRIES:
            out[r] = await cache.refresh(r)
        return {"mode": (await cache.status())["mode"], "registries": out}
    for reg in REGISTRIES:
        if registry == reg:
            return await cache.refresh(reg)
    raise HTTPException(422, f"unknown registry {registry!r}; one of {list(REGISTRIES)}/all")
