"""Seed data (spec §9): idempotent static load — servers, native skills from
.skill.md, native tool registration, research-concierge sub agent."""

from typing import Any

from httpx import AsyncClient

API = "/api/v1"


async def test_seed_contents(seeded_client: AsyncClient) -> None:
    client = seeded_client

    servers = (await client.get(f"{API}/mcp-servers")).json()
    by_name = {s["name"]: s for s in servers}
    assert {"fetch", "filesystem"} <= set(by_name)
    assert by_name["fetch"]["source"] == "static"
    assert by_name["fetch"]["transport"] == "stdio"
    assert by_name["filesystem"]["transport"] == "stdio"

    skills = (await client.get(f"{API}/skills")).json()
    skills_by_name = {s["name"]: s for s in skills}
    assert {"web-research", "file-ops"} <= set(skills_by_name)
    web = skills_by_name["web-research"]
    assert web["kind"] == "native" and web["source"] == "static"
    assert web["persona"]
    assert "{tool:" in web["instructions"]

    tools = (await client.get(f"{API}/tools")).json()
    native = [t for t in tools if t["kind"] == "native"]
    assert any(t["tool_key"] == "summarize-and-structure" for t in native)
    summarize = next(t for t in native if t["tool_key"] == "summarize-and-structure")
    assert summarize["source"] == "static"
    assert summarize["native_ref"]
    # bound into web-research (spec §5b)
    tool_skills = (await client.get(f"{API}/tools/{summarize['id']}/skills")).json()
    assert any(s["name"] == "web-research" for s in tool_skills)

    agents = (await client.get(f"{API}/sub-agents")).json()
    concierge = next(a for a in agents if a["name"] == "research-concierge")
    assert concierge["source"] == "static"
    workflow = concierge["workflow"]
    node_types = {n["type"] for n in workflow["nodes"]}
    assert "hitl" in node_types and "skill" in node_types
    conditions = [e.get("condition") for e in workflow["edges"] if e.get("condition")]
    assert len(conditions) >= 2  # branch: found results / nothing found

    settings = (await client.get(f"{API}/settings")).json()
    assert settings["orchestrator_mode"] == "graph"


async def test_seed_idempotent(seeded_client: AsyncClient) -> None:
    client = seeded_client

    def summary(items: list[dict[str, Any]]) -> set[tuple[str, str]]:
        return {(i["id"], i["name"]) for i in items}

    before_skills = summary((await client.get(f"{API}/skills")).json())
    before_servers = summary((await client.get(f"{API}/mcp-servers")).json())
    before_agents = summary((await client.get(f"{API}/sub-agents")).json())

    resp = await client.post(f"{API}/seed/reload")
    assert resp.status_code == 200

    assert summary((await client.get(f"{API}/skills")).json()) == before_skills
    assert summary((await client.get(f"{API}/mcp-servers")).json()) == before_servers
    assert summary((await client.get(f"{API}/sub-agents")).json()) == before_agents
