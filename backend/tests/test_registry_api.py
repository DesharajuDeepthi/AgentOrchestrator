"""Registry API contract (spec §4): CRUD, filters, static 403s, 409 dependents,
strict skill_id references, DAG save validation, settings validation."""

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

API = "/api/v1"


async def make_server(client: AsyncClient, name: str = "test-server") -> dict[str, Any]:
    resp = await client.post(
        f"{API}/mcp-servers",
        json={
            "name": name,
            "description": "a test stdio server",
            "transport": "stdio",
            "command": "echo",
            "args": ["hi"],
            "env": {"FOO": "bar"},
        },
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def make_tool_row(session: Any, server_id: str | None = None, **kw: Any) -> str:
    """Insert a tool row directly (MCP ingestion is M2)."""
    from uuid import UUID

    from app.models import Tool

    defaults: dict[str, Any] = {
        "name": kw.get("tool_name", "demo_tool"),
        "kind": "mcp" if server_id else "native",
        "tool_name": "demo_tool",
        "tool_key": f"srv.demo_tool_{uuid4().hex[:6]}",
        "source": "dynamic",
        "input_schema": {"type": "object", "properties": {}},
    }
    defaults.update(kw)
    if server_id:
        defaults["mcp_server_id"] = UUID(server_id)
    tool = Tool(**defaults)
    session.add(tool)
    await session.commit()
    return str(tool.id)


async def make_skill(
    client: AsyncClient,
    name: str = "test-skill",
    tool_ids: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "description": "a test skill",
        "persona": "You are a tester.",
        "instructions": "# Purpose\nDo the thing.",
        "tool_ids": tool_ids or [],
        "direct_exposure": False,
    }
    payload.update(overrides)
    resp = await client.post(f"{API}/skills", json=payload)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def seq_workflow(skill_id: str) -> dict[str, Any]:
    return {
        "nodes": [{"id": "n1", "type": "skill", "skill_id": skill_id}],
        "edges": [{"from": "START", "to": "n1"}, {"from": "n1", "to": "END"}],
    }


async def make_sub_agent(
    client: AsyncClient, skill_id: str, name: str = "test-agent", **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "description": "a test sub agent",
        "persona": "You are a helpful worker.",
        "workflow": seq_workflow(skill_id),
    }
    payload.update(overrides)
    resp = await client.post(f"{API}/sub-agents", json=payload)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


class TestMcpServerCrud:
    async def test_create_and_get(self, client: AsyncClient) -> None:
        created = await make_server(client)
        assert created["source"] == "dynamic"
        resp = await client.get(f"{API}/mcp-servers/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["command"] == "echo"

    async def test_list_filters(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        await make_server(client, name="filter-me")
        listed = (await client.get(f"{API}/mcp-servers", params={"source": "dynamic"})).json()
        assert all(s["source"] == "dynamic" for s in listed)
        assert any(s["name"] == "filter-me" for s in listed)
        q = (await client.get(f"{API}/mcp-servers", params={"q": "filter"})).json()
        assert len(q) == 1 and q[0]["name"] == "filter-me"

    async def test_soft_delete(self, client: AsyncClient) -> None:
        created = await make_server(client)
        resp = await client.delete(f"{API}/mcp-servers/{created['id']}")
        assert resp.status_code == 204
        listed = (await client.get(f"{API}/mcp-servers")).json()
        assert all(s["id"] != created["id"] for s in listed)
        with_deleted = (
            await client.get(f"{API}/mcp-servers", params={"include_deleted": "true"})
        ).json()
        assert any(s["id"] == created["id"] for s in with_deleted)

    async def test_static_content_patch_rejected(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        servers = (await client.get(f"{API}/mcp-servers", params={"source": "static"})).json()
        fetch = next(s for s in servers if s["name"] == "fetch")
        resp = await client.patch(f"{API}/mcp-servers/{fetch['id']}", json={"command": "evil"})
        assert resp.status_code == 403
        assert "static" in resp.json()["detail"].lower()

    async def test_static_status_toggle_allowed(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        servers = (await client.get(f"{API}/mcp-servers", params={"source": "static"})).json()
        fetch = next(s for s in servers if s["name"] == "fetch")
        resp = await client.patch(f"{API}/mcp-servers/{fetch['id']}", json={"status": "inactive"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "inactive"

    async def test_static_delete_rejected(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        servers = (await client.get(f"{API}/mcp-servers", params={"source": "static"})).json()
        resp = await client.delete(f"{API}/mcp-servers/{servers[0]['id']}")
        assert resp.status_code == 403

    async def test_delete_with_bound_tools_conflict(
        self, client: AsyncClient, session: Any
    ) -> None:
        server = await make_server(client)
        tool_id = await make_tool_row(session, server_id=server["id"])
        await make_skill(client, tool_ids=[tool_id])
        resp = await client.delete(f"{API}/mcp-servers/{server['id']}")
        assert resp.status_code == 409
        assert "test-skill" in resp.text


class TestToolsApi:
    async def test_patch_allowed_fields(self, client: AsyncClient, session: Any) -> None:
        tool_id = await make_tool_row(session)
        resp = await client.patch(
            f"{API}/tools/{tool_id}",
            json={"description": "updated", "direct_exposure": True, "status": "inactive"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["description"] == "updated"
        assert body["direct_exposure"] is True

    async def test_patch_schema_rejected(self, client: AsyncClient, session: Any) -> None:
        tool_id = await make_tool_row(session)
        resp = await client.patch(
            f"{API}/tools/{tool_id}", json={"input_schema": {"type": "object"}}
        )
        assert resp.status_code == 422

    async def test_static_tool_description_rejected(
        self, client: AsyncClient, session: Any
    ) -> None:
        tool_id = await make_tool_row(session, source="static")
        resp = await client.patch(f"{API}/tools/{tool_id}", json={"description": "nope"})
        assert resp.status_code == 403
        resp = await client.patch(
            f"{API}/tools/{tool_id}", json={"direct_exposure": True, "status": "inactive"}
        )
        assert resp.status_code == 200

    async def test_tool_key_rename_keeps_bindings(self, client: AsyncClient, session: Any) -> None:
        tool_id = await make_tool_row(session)
        skill = await make_skill(client, tool_ids=[tool_id])
        resp = await client.patch(f"{API}/tools/{tool_id}", json={"tool_key": "renamed.key"})
        assert resp.status_code == 200
        skills_of_tool = (await client.get(f"{API}/tools/{tool_id}/skills")).json()
        assert [s["id"] for s in skills_of_tool] == [skill["id"]]

    async def test_tool_key_collision_rejected(self, client: AsyncClient, session: Any) -> None:
        await make_tool_row(session, tool_key="taken.key")
        other = await make_tool_row(session)
        resp = await client.patch(f"{API}/tools/{other}", json={"tool_key": "taken.key"})
        assert resp.status_code == 409


class TestSkillsApi:
    async def test_create_with_bindings_and_mentions(
        self, client: AsyncClient, session: Any
    ) -> None:
        tool_id = await make_tool_row(session, tool_key="srv.fetch")
        skill = await make_skill(
            client,
            tool_ids=[tool_id],
            instructions="1. Call {tool:srv.fetch}.\n2. Summarize.",
        )
        assert [t["id"] for t in skill["tools"]] == [tool_id]

    async def test_mention_of_unbound_tool_rejected(
        self, client: AsyncClient, session: Any
    ) -> None:
        tool_id = await make_tool_row(session, tool_key="srv.fetch")
        resp = await client.post(
            f"{API}/skills",
            json={
                "name": "bad-mentions",
                "description": "x",
                "persona": "p",
                "instructions": "Call {tool:not.bound}",
                "tool_ids": [tool_id],
            },
        )
        assert resp.status_code == 422
        assert "not.bound" in resp.text

    async def test_unknown_tool_id_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{API}/skills",
            json={
                "name": "ghost-tools",
                "description": "x",
                "persona": "p",
                "instructions": "i",
                "tool_ids": [str(uuid4())],
            },
        )
        assert resp.status_code == 422

    async def test_inactive_tool_id_rejected(self, client: AsyncClient, session: Any) -> None:
        tool_id = await make_tool_row(session, status="inactive")
        resp = await client.post(
            f"{API}/skills",
            json={
                "name": "inactive-tool-skill",
                "description": "x",
                "persona": "p",
                "instructions": "i",
                "tool_ids": [tool_id],
            },
        )
        assert resp.status_code == 422

    async def test_skill_model_override_with_params(self, client: AsyncClient) -> None:
        skill = await make_skill(
            client,
            name="model-override-skill",
            model="fake:scripted",
            model_params={"effort": "high", "temperature": 0.1},
        )
        assert skill["model"] == "fake:scripted"
        assert skill["model_params"]["effort"] == "high"

    async def test_skill_model_unconfigured_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{API}/skills",
            json={
                "name": "anthropic-skill",
                "description": "x",
                "persona": "p",
                "instructions": "i",
                "tool_ids": [],
                "model": "anthropic:claude-sonnet-4-6",
            },
        )
        assert resp.status_code == 422

    async def test_skill_model_params_without_model_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{API}/skills",
            json={
                "name": "params-only-skill",
                "description": "x",
                "persona": "p",
                "instructions": "i",
                "tool_ids": [],
                "model_params": {"effort": "high"},
            },
        )
        assert resp.status_code == 422

    async def test_static_skill_content_locked(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        skills = (await client.get(f"{API}/skills", params={"q": "web-research"})).json()
        skill = skills[0]
        assert skill["source"] == "static"
        resp = await client.patch(f"{API}/skills/{skill['id']}", json={"persona": "evil"})
        assert resp.status_code == 403
        resp = await client.patch(f"{API}/skills/{skill['id']}", json={"direct_exposure": True})
        assert resp.status_code == 200

    async def test_delete_with_dependent_sub_agent_conflict(self, client: AsyncClient) -> None:
        skill = await make_skill(client)
        agent = await make_sub_agent(client, skill["id"])
        resp = await client.delete(f"{API}/skills/{skill['id']}")
        assert resp.status_code == 409
        assert agent["name"] in resp.text
        await client.delete(f"{API}/sub-agents/{agent['id']}")
        resp = await client.delete(f"{API}/skills/{skill['id']}")
        assert resp.status_code == 204

    async def test_reverse_lookup_sub_agents(self, client: AsyncClient) -> None:
        skill = await make_skill(client)
        agent = await make_sub_agent(client, skill["id"])
        got = (await client.get(f"{API}/skills/{skill['id']}/sub-agents")).json()
        assert [a["id"] for a in got] == [agent["id"]]

    async def test_delete_tool_bound_to_skill_conflict(
        self, client: AsyncClient, session: Any
    ) -> None:
        tool_id = await make_tool_row(session)
        skill = await make_skill(client, tool_ids=[tool_id])
        resp = await client.delete(f"{API}/tools/{tool_id}")
        assert resp.status_code == 409
        assert skill["name"] in resp.text


class TestSubAgentValidation:
    async def test_valid_branch_error_hitl_dag(self, client: AsyncClient) -> None:
        s1 = await make_skill(client, name="research")
        s2 = await make_skill(client, name="write")
        workflow = {
            "nodes": [
                {"id": "research", "type": "skill", "skill_id": s1["id"]},
                {"id": "approve", "type": "hitl", "prompt": "Save findings?"},
                {"id": "write", "type": "skill", "skill_id": s2["id"]},
                {"id": "recover", "type": "skill", "skill_id": s2["id"]},
            ],
            "edges": [
                {"from": "START", "to": "research"},
                {"from": "research", "to": "approve", "condition": "if found results"},
                {"from": "research", "to": "END", "condition": "if nothing found"},
                {"from": "research", "to": "recover", "on": "error"},
                {"from": "recover", "to": "END"},
                {"from": "approve", "to": "write"},
                {"from": "write", "to": "END"},
            ],
        }
        agent = await make_sub_agent(client, s1["id"], name="dag-agent", workflow=workflow)
        assert {s["id"] for s in agent["skills"]} == {s1["id"], s2["id"]}

    @staticmethod
    def _remove_end_edges(w: dict[str, Any]) -> None:
        w["edges"][:] = [e for e in w["edges"] if e["to"] != "END"]

    @pytest.mark.parametrize(
        "mutate,expected_fragment",
        [
            (lambda w: w["edges"].pop(0), "START"),
            (lambda w: w["edges"].append({"from": "START", "to": "n1"}), "START"),
            (lambda w: w["edges"].append({"from": "n1", "to": "n1"}), "cycle"),
            (lambda w: w["edges"].append({"from": "n1", "to": "ghost"}), "ghost"),
            (lambda w: w["nodes"].append(dict(w["nodes"][0])), "unique"),
            (_remove_end_edges, "END"),
        ],
    )
    async def test_invalid_dags_rejected(
        self,
        client: AsyncClient,
        mutate: Any,
        expected_fragment: str,
    ) -> None:
        skill = await make_skill(client, name=f"skill-{uuid4().hex[:6]}")
        workflow = seq_workflow(skill["id"])
        workflow["nodes"].append({"id": "n1x", "type": "hitl", "prompt": "ok?"})
        workflow["edges"].insert(1, {"from": "n1", "to": "n1x"})
        workflow["edges"].append({"from": "n1x", "to": "END"})
        # baseline is valid; then break it
        mutate(workflow)
        resp = await client.post(
            f"{API}/sub-agents",
            json={
                "name": f"bad-{uuid4().hex[:6]}",
                "description": "x",
                "persona": "p",
                "workflow": workflow,
            },
        )
        assert resp.status_code == 422, resp.text
        assert expected_fragment.lower() in resp.text.lower()

    async def test_unknown_skill_id_rejected_422(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{API}/sub-agents",
            json={
                "name": "ghost-skill-agent",
                "description": "x",
                "persona": "p",
                "workflow": seq_workflow(str(uuid4())),
            },
        )
        assert resp.status_code == 422

    async def test_inactive_skill_rejected(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="soon-inactive")
        await client.patch(f"{API}/skills/{skill['id']}", json={"status": "inactive"})
        resp = await client.post(
            f"{API}/sub-agents",
            json={
                "name": "inactive-skill-agent",
                "description": "x",
                "persona": "p",
                "workflow": seq_workflow(skill["id"]),
            },
        )
        assert resp.status_code == 422

    async def test_two_error_edges_rejected(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="err-skill")
        workflow = seq_workflow(skill["id"])
        workflow["nodes"].append({"id": "h", "type": "hitl", "prompt": "ok?"})
        workflow["edges"] += [
            {"from": "n1", "to": "h", "on": "error"},
            {"from": "n1", "to": "END", "on": "error"},
            {"from": "h", "to": "END"},
        ]
        resp = await client.post(
            f"{API}/sub-agents",
            json={"name": "two-errs", "description": "x", "persona": "p", "workflow": workflow},
        )
        assert resp.status_code == 422
        assert "error" in resp.text.lower()

    async def test_hitl_without_prompt_rejected(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="hitl-skill")
        workflow = seq_workflow(skill["id"])
        workflow["nodes"].append({"id": "h", "type": "hitl"})
        workflow["edges"].insert(1, {"from": "n1", "to": "h"})
        workflow["edges"].append({"from": "h", "to": "END"})
        resp = await client.post(
            f"{API}/sub-agents",
            json={"name": "no-prompt", "description": "x", "persona": "p", "workflow": workflow},
        )
        assert resp.status_code == 422

    async def test_validate_endpoint(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="validate-me")
        agent = await make_sub_agent(client, skill["id"], name="validatable")
        resp = await client.post(f"{API}/sub-agents/{agent['id']}/validate")
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    async def test_model_override_unconfigured_provider_rejected(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="model-skill")
        resp = await client.post(
            f"{API}/sub-agents",
            json={
                "name": "anthropic-agent",
                "description": "x",
                "persona": "p",
                "model": "anthropic:claude-sonnet-4-6",
                "workflow": seq_workflow(skill["id"]),
            },
        )
        assert resp.status_code == 422

    async def test_model_override_configured_ok(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="fake-model-skill")
        agent = await make_sub_agent(
            client,
            skill["id"],
            name="fake-agent",
            model="fake:scripted",
            model_params={"effort": "medium", "max_output_tokens": 900},
        )
        assert agent["model"] == "fake:scripted"
        assert agent["model_params"]["effort"] == "medium"

    async def test_model_params_bad_effort_rejected(self, client: AsyncClient) -> None:
        skill = await make_skill(client, name="bad-effort-skill")
        resp = await client.post(
            f"{API}/sub-agents",
            json={
                "name": "bad-effort-agent",
                "description": "x",
                "persona": "p",
                "model": "fake:scripted",
                "model_params": {"effort": "extreme"},
                "workflow": seq_workflow(skill["id"]),
            },
        )
        assert resp.status_code == 422

    async def test_static_sub_agent_locked(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        agents = (await client.get(f"{API}/sub-agents", params={"q": "research-concierge"})).json()
        agent = agents[0]
        resp = await client.patch(f"{API}/sub-agents/{agent['id']}", json={"persona": "evil"})
        assert resp.status_code == 403
        resp = await client.patch(f"{API}/sub-agents/{agent['id']}", json={"status": "inactive"})
        assert resp.status_code == 200


class TestSettings:
    async def test_defaults_present(self, seeded_client: AsyncClient) -> None:
        settings = (await seeded_client.get(f"{API}/settings")).json()
        assert settings["orchestrator_mode"] == "graph"
        # first-boot resolution (spec §13): anthropic is unconfigured in the
        # test env, so the seed pass resolves the fake flagship
        assert settings["default_model"] == "fake:scripted"
        assert settings["orchestrator_full_fallback_enabled"] is True
        assert isinstance(settings["max_tool_iterations"], int)

    async def test_patch_setting_live(self, seeded_client: AsyncClient) -> None:
        client = seeded_client
        resp = await client.patch(f"{API}/settings", json={"max_plan_steps": 5})
        assert resp.status_code == 200
        assert (await client.get(f"{API}/settings")).json()["max_plan_steps"] == 5

    async def test_patch_unconfigured_model_rejected(self, seeded_client: AsyncClient) -> None:
        resp = await seeded_client.patch(
            f"{API}/settings", json={"default_model": "openai:gpt-5-mini"}
        )
        assert resp.status_code == 422

    async def test_patch_configured_model_ok(self, seeded_client: AsyncClient) -> None:
        resp = await seeded_client.patch(f"{API}/settings", json={"default_model": "fake:scripted"})
        assert resp.status_code == 200

    async def test_patch_bad_mode_rejected(self, seeded_client: AsyncClient) -> None:
        resp = await seeded_client.patch(f"{API}/settings", json={"orchestrator_mode": "bogus"})
        assert resp.status_code == 422

    async def test_patch_unknown_key_rejected(self, seeded_client: AsyncClient) -> None:
        resp = await seeded_client.patch(f"{API}/settings", json={"nonsense_key": 1})
        assert resp.status_code == 422

    async def test_patch_model_params_validated_against_model(
        self, seeded_client: AsyncClient
    ) -> None:
        client = seeded_client
        resp = await client.patch(
            f"{API}/settings",
            json={"default_model": "fake:scripted", "default_model_params": {"effort": "high"}},
        )
        assert resp.status_code == 200
        assert (await client.get(f"{API}/settings")).json()["default_model_params"] == {
            "effort": "high"
        }
        resp = await client.patch(
            f"{API}/settings", json={"default_model_params": {"effort": "extreme"}}
        )
        assert resp.status_code == 422
        resp = await client.patch(f"{API}/settings", json={"default_model_params": {"bogus": 1}})
        assert resp.status_code == 422


class TestProvidersPanel:
    async def test_providers_listed_with_configured_status(self, client: AsyncClient) -> None:
        providers = (await client.get(f"{API}/providers")).json()
        by_id = {p["provider_id"]: p for p in providers}
        assert {"anthropic", "google_genai", "openai", "fake"} <= set(by_id)
        assert by_id["fake"]["configured"] is True  # FAKE_LLM_ENABLED in tests
        assert by_id["anthropic"]["configured"] is False
        model = by_id["anthropic"]["models"][0]
        assert {"id", "display_name", "supports_effort"} <= set(model)


class TestFakeScriptControl:
    async def test_script_endpoint_gated_and_queues(
        self, client: AsyncClient, monkeypatch: Any
    ) -> None:
        from app.config import get_config
        from app.llm import fake as fake_llm

        resp = await client.post(f"{API}/_fake/script", json={"calls": [{"content": "hi"}]})
        assert resp.status_code == 200  # FAKE_LLM_ENABLED=1 in tests
        assert fake_llm.script_len() == 1
        await client.post(f"{API}/_fake/clear")
        assert fake_llm.script_len() == 0

        monkeypatch.setenv("FAKE_LLM_ENABLED", "0")
        get_config.cache_clear()
        try:
            resp = await client.post(f"{API}/_fake/script", json={"calls": []})
            assert resp.status_code == 404  # invisible when disabled
        finally:
            get_config.cache_clear()
