"""Seeded native tier (spec §9 additions): workspace-auditor +
workspace-curator native skills over previously-untagged filesystem tools,
and the workspace-warden native sub agent (spec §3.4) over both — shipped
exposed and invokable through every §7.5 surface."""

import asyncio
from typing import Any

from httpx import AsyncClient

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.settings_store import update_settings

API = "/api/v1"


async def _wait_run(
    client: AsyncClient, run_id: str, statuses: set[str], timeout_s: float = 20.0
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    run: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        run = (await client.get(f"{API}/runs/{run_id}")).json()
        if run["status"] in statuses:
            return dict(run)
        await asyncio.sleep(0.1)
    raise AssertionError(f"run did not reach {statuses}; last: {run.get('status')}")


async def test_seeded_native_skills_present(seeded_client: AsyncClient) -> None:
    skills = {s["name"]: s for s in (await seeded_client.get(f"{API}/skills")).json()}
    for name in ("workspace-auditor", "workspace-curator"):
        assert name in skills, f"{name} missing from seeded registry"
        skill = skills[name]
        assert skill["kind"] == "native" and skill["source"] == "static"
        assert skill["persona"]
        assert "{tool:" in skill["instructions"]


async def test_workspace_warden_seeded_and_covers_resolved(seeded_client: AsyncClient) -> None:
    agents = (await seeded_client.get(f"{API}/sub-agents")).json()
    warden = next((a for a in agents if a["name"] == "workspace-warden"), None)
    assert warden is not None, "workspace-warden missing from seeded registry"
    assert warden["kind"] == "native" and warden["source"] == "static"
    assert warden["direct_exposure"] is True  # static seeds ship exposed
    assert warden["native_ref"].endswith("build_workspace_warden")
    skills = {s["name"]: s for s in (await seeded_client.get(f"{API}/skills")).json()}
    expected = {skills["workspace-auditor"]["id"], skills["workspace-curator"]["id"]}
    # names in code resolved to registry uuids at seed time
    assert set(warden["covers_skill_ids"]) == expected


async def test_warden_direct_invoke_two_stages(seeded_client: AsyncClient) -> None:
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {"default_model": "fake:scripted", "formatter_enabled": False},
        )
    agents = (await seeded_client.get(f"{API}/sub-agents")).json()
    warden = next(a for a in agents if a["name"] == "workspace-warden")
    fake_llm.push_ai("AUDIT: 3 files, tree mapped, largest is notes.md (4 KB)")
    fake_llm.push_ai("CURATE: created docs/, moved notes.md -> docs/notes.md")
    resp = await seeded_client.post(
        f"{API}/sub-agents/{warden['id']}/invoke",
        json={"message": "audit the workspace then tidy loose files"},
    )
    assert resp.status_code == 201, resp.text
    run = await _wait_run(seeded_client, resp.json()["run_id"], {"completed", "failed"})
    assert run["status"] == "completed", run["error"]
    assert run["orchestrator_mode"] == "direct"
    assert run["target_sub_agent_id"] == warden["id"]
    # both stages' outputs merge into the final answer, audit before curate
    assert "AUDIT:" in run["final_answer"] and "CURATE:" in run["final_answer"]
    assert run["final_answer"].index("AUDIT:") < run["final_answer"].index("CURATE:")
    rungs = [
        s["output"]["rung"]
        for s in run["steps"]
        if s["step_type"] == "route" and (s.get("output") or {}).get("rung")
    ]
    assert rungs == ["native_sub_agent"]
    # each stage recorded as a step carrying its registry skill identity
    stage_steps = {s["node_id"]: s for s in run["steps"] if s["node_id"] in {"audit", "curate"}}
    assert set(stage_steps) == {"audit", "curate"}
    skills = {s["name"]: s for s in (await seeded_client.get(f"{API}/skills")).json()}
    assert stage_steps["audit"]["output"]["skill_id"] == skills["workspace-auditor"]["id"]
    assert stage_steps["curate"]["output"]["skill_id"] == skills["workspace-curator"]["id"]


async def test_warden_chat_target_surface(seeded_client: AsyncClient) -> None:
    async with get_session_factory()() as session:
        await update_settings(
            session,
            {"default_model": "fake:scripted", "formatter_enabled": False},
        )
    agents = (await seeded_client.get(f"{API}/sub-agents")).json()
    warden = next(a for a in agents if a["name"] == "workspace-warden")
    fake_llm.push_ai("audit out")
    fake_llm.push_ai("curate out")
    resp = await seeded_client.post(
        f"{API}/chat",
        json={"message": "tidy up please", "target_sub_agent_id": warden["id"]},
    )
    assert resp.status_code == 201, resp.text
    run = await _wait_run(seeded_client, resp.json()["run_id"], {"completed", "failed"})
    assert run["status"] == "completed", run["error"]
    assert run["orchestrator_mode"] == "direct"
