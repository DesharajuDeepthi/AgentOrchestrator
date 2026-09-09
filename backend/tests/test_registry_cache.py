"""Registry cache layer (spec §7.3): backend contract suite.

Every test in ``TestCacheContract`` runs identically against bypass and
memory — same typed reads, same invalidation-after-write ordering, same
refresh/status shapes. Redis is env-gated (see TestRedisBackend).
"""

import os

import pytest
from httpx import AsyncClient

from app.db import get_session_factory
from app.registry_cache import get_cache
from app.settings_store import update_settings
from tests.factory_helpers import create_skill, create_sub_agent, create_tool


@pytest.fixture(params=["bypass", "memory"])
async def cache_mode(request: pytest.FixtureRequest) -> str:
    async with get_session_factory()() as session:
        await update_settings(session, {"registry_cache_mode": request.param})
    return str(request.param)


class TestCacheContract:
    async def test_tools_reads(self, cache_mode: str) -> None:
        exposed = await create_tool(direct_exposure=True)
        hidden = await create_tool(direct_exposure=False)
        inactive = await create_tool(direct_exposure=True, status="inactive")
        cache = get_cache()
        all_ids = {t["id"] for t in await cache.tools(exposed_only=False)}
        assert {str(exposed.id), str(hidden.id)} <= all_ids
        assert str(inactive.id) not in all_ids
        exposed_ids = {t["id"] for t in await cache.tools(exposed_only=True)}
        assert str(exposed.id) in exposed_ids
        assert str(hidden.id) not in exposed_ids
        # by-ids: order-preserving, inactive dropped
        got = await cache.tools_by_ids([hidden.id, inactive.id, exposed.id])
        assert [t["id"] for t in got] == [str(hidden.id), str(exposed.id)]

    async def test_skill_snapshot_shape(self, cache_mode: str) -> None:
        tool = await create_tool()
        skill = await create_skill(tools=[tool], direct_exposure=True)
        cache = get_cache()
        snap = await cache.skill_by_id(skill.id)
        assert snap is not None
        assert snap["name"] == skill.name
        assert snap["persona"] == skill.persona
        assert [t["id"] for t in snap["tools"]] == [str(tool.id)]
        assert snap["id"] in {s["id"] for s in await cache.skills(exposed_only=True)}

    async def test_sub_agent_cards_and_snapshot(self, cache_mode: str) -> None:
        skill = await create_skill()
        workflow = {
            "nodes": [{"id": "n1", "type": "skill", "skill_id": str(skill.id)}],
            "edges": [{"from": "START", "to": "n1"}, {"from": "n1", "to": "END"}],
        }
        agent = await create_sub_agent(workflow, skills=[skill])
        cache = get_cache()
        card = next(c for c in await cache.sub_agent_cards() if c["id"] == str(agent.id))
        assert card["skills"] == [skill.name]
        snap = await cache.sub_agent_snapshot(agent.id)
        assert snap is not None
        assert snap["workflow"] == workflow
        assert str(skill.id) in snap["skills"]

    async def test_settings_read(self, cache_mode: str) -> None:
        assert await get_cache().setting("max_tool_iterations") == 8
        assert await get_cache().setting("registry_cache_mode") == cache_mode

    async def test_write_invalidates_before_return(
        self, cache_mode: str, client: AsyncClient
    ) -> None:
        """Spec §7.3: event-invalidated — a PATCH is visible on the next read."""
        tool = await create_tool(direct_exposure=False)
        cache = get_cache()
        assert str(tool.id) not in {t["id"] for t in await cache.tools(exposed_only=True)}
        resp = await client.patch(f"/api/v1/tools/{tool.id}", json={"direct_exposure": True})
        assert resp.status_code == 200
        assert str(tool.id) in {t["id"] for t in await cache.tools(exposed_only=True)}

    async def test_tool_write_propagates_to_skill_snapshots(
        self, cache_mode: str, client: AsyncClient
    ) -> None:
        """Skills embed tool rows — a tool PATCH must dirty the skills registry."""
        tool = await create_tool()
        skill = await create_skill(tools=[tool])
        cache = get_cache()
        snap = await cache.skill_by_id(skill.id)
        assert snap is not None and snap["tools"][0]["description"] == ""
        resp = await client.patch(f"/api/v1/tools/{tool.id}", json={"description": "updated!"})
        assert resp.status_code == 200
        snap = await cache.skill_by_id(skill.id)
        assert snap is not None and snap["tools"][0]["description"] == "updated!"

    async def test_settings_patch_visible_immediately(
        self, cache_mode: str, client: AsyncClient
    ) -> None:
        resp = await client.patch("/api/v1/settings", json={"max_tool_iterations": 3})
        assert resp.status_code == 200
        assert await get_cache().setting("max_tool_iterations") == 3

    async def test_refresh_endpoint_and_status(self, cache_mode: str, client: AsyncClient) -> None:
        await create_tool()
        status = (await client.get("/api/v1/cache/status")).json()
        assert status["mode"] == cache_mode
        assert set(status["registries"]) == {"tools", "skills", "sub_agents", "settings"}
        refreshed = (await client.post("/api/v1/cache/refresh/tools")).json()
        assert refreshed["records"] >= 1
        assert refreshed["cached"] is (cache_mode != "bypass")
        all_refreshed = (await client.post("/api/v1/cache/refresh/all")).json()
        assert set(all_refreshed["registries"]) == {"tools", "skills", "sub_agents", "settings"}
        assert (await client.post("/api/v1/cache/refresh/nope")).status_code == 422

    async def test_mode_flip_applies_live(self, client: AsyncClient) -> None:
        """bypass → memory → bypass via PATCH, no restart (spec §7.3)."""
        tool = await create_tool()
        cache = get_cache()
        assert (await cache.status())["mode"] == "bypass"
        resp = await client.patch("/api/v1/settings", json={"registry_cache_mode": "memory"})
        assert resp.status_code == 200
        assert (await cache.status())["mode"] == "memory"
        assert str(tool.id) in {t["id"] for t in await cache.tools(exposed_only=False)}
        status = await cache.status()
        assert status["registries"]["tools"]["cached"] is True
        resp = await client.patch("/api/v1/settings", json={"registry_cache_mode": "bypass"})
        assert resp.status_code == 200
        assert (await cache.status())["mode"] == "bypass"

    @pytest.mark.skipif(bool(os.environ.get("REDIS_URL")), reason="REDIS_URL is set")
    async def test_redis_mode_rejected_without_url(self, client: AsyncClient) -> None:
        resp = await client.patch("/api/v1/settings", json={"registry_cache_mode": "redis"})
        assert resp.status_code == 422
        assert "REDIS_URL" in resp.text


@pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="REDIS_URL not set")
class TestRedisBackend:
    """Env-gated (spec §7.3): excluded from the default test gate."""

    async def test_redis_round_trip(self, client: AsyncClient) -> None:
        from app.config import get_config

        get_config.cache_clear()
        tool = await create_tool()
        resp = await client.patch("/api/v1/settings", json={"registry_cache_mode": "redis"})
        assert resp.status_code == 200, resp.text
        cache = get_cache()
        assert str(tool.id) in {t["id"] for t in await cache.tools(exposed_only=False)}
        resp = await client.patch(f"/api/v1/tools/{tool.id}", json={"description": "via redis"})
        assert resp.status_code == 200
        got = await cache.tool_by_id(tool.id)
        assert got is not None and got["description"] == "via redis"


class TestCrossReplicaSync:
    """Spec §7.3 LISTEN/NOTIFY: dormant on a single node, functional when a
    peer broadcasts. Simulated here with a foreign-origin pg_notify."""

    async def test_foreign_notify_marks_dirty(self) -> None:
        import asyncio

        from sqlalchemy import text

        cache = get_cache()
        await cache.start_listener()
        try:
            gen_before = (await cache.status())["registries"]["tools"]["generation"]
            async with get_session_factory()() as session:
                await session.execute(
                    text("SELECT pg_notify('registry_cache_inv', 'peer-origin:tools')")
                )
                await session.commit()
            for _ in range(50):
                await asyncio.sleep(0.1)
                gen_now = (await cache.status())["registries"]["tools"]["generation"]
                if gen_now > gen_before:
                    break
            assert gen_now > gen_before, "peer notification must dirty the local cache"
            # propagation still applies on the listener path
            assert (await cache.status())["registries"]["skills"]["generation"] >= 1
        finally:
            await cache.stop_listener()

    async def test_own_notifications_are_filtered(self) -> None:
        import asyncio

        cache = get_cache()
        await cache.start_listener()
        try:
            await cache.invalidate("tools")
            gen_after = (await cache.status())["registries"]["tools"]["generation"]
            await asyncio.sleep(0.5)  # a self-echo would bump again
            assert (await cache.status())["registries"]["tools"]["generation"] == gen_after
        finally:
            await cache.stop_listener()
