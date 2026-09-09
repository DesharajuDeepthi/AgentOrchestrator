"""Declarative .agent.md static sub-agents (spec §3.4).

Contract: definition follows the file; skill-by-name resolves at seed;
invalid files land as status='error' without crashing the seed; user
toggles survive reseeds; removed files deactivate; the seeded
workspace-reporter runs end to end through direct invocation with its
form gate."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.agentdoc import AgentDocError, parse_agent_document
from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import SubAgent
from app.seed.loader import seed_agent_files
from app.settings_store import update_settings
from tests.factory_helpers import create_skill

API = "/api/v1"

VALID_DOC = """---
name: {name}
description: test agent from file
persona: file persona
direct_exposure: true
workflow:
  nodes:
    - id: work
      type: skill
      skill: {skill}
  edges:
    - {{ from: START, to: work }}
    - {{ from: work, to: END }}
---
# Notes
body is documentation only
"""


class TestParser:
    def test_valid_document(self) -> None:
        doc = parse_agent_document(VALID_DOC.format(name="a", skill="s"), "a.agent.md")
        assert doc.name == "a"
        assert doc.direct_exposure is True
        assert doc.workflow["nodes"][0]["skill"] == "s"
        assert "documentation only" in doc.notes
        assert doc.filename == "a.agent.md"

    def test_missing_frontmatter(self) -> None:
        with pytest.raises(AgentDocError, match="frontmatter"):
            parse_agent_document("# just markdown")

    def test_missing_workflow(self) -> None:
        with pytest.raises(AgentDocError, match="workflow"):
            parse_agent_document("---\nname: x\n---\nbody")

    def test_skill_node_without_ref(self) -> None:
        bad = (
            "---\nname: x\nworkflow:\n  nodes:\n    - {id: n, type: skill}\n"
            "  edges:\n    - {from: START, to: n}\n---\n"
        )
        with pytest.raises(AgentDocError, match="skill"):
            parse_agent_document(bad)


class TestSeededReporter:
    async def test_seeded_and_resolved(self, seeded_client: AsyncClient) -> None:
        agents = (await seeded_client.get(f"{API}/sub-agents")).json()
        rep = next((a for a in agents if a["name"] == "workspace-reporter"), None)
        assert rep is not None, "workspace-reporter missing from seed"
        assert rep["kind"] == "custom" and rep["source"] == "static"
        assert rep["status"] == "active"
        assert rep["direct_exposure"] is True
        assert rep["native_ref"] == "file:workspace-reporter.agent.md"
        skills = {s["name"]: s for s in (await seeded_client.get(f"{API}/skills")).json()}
        for node in rep["workflow"]["nodes"]:
            assert "skill" not in node  # by-name sugar fully resolved away
            if node["type"] == "skill":
                assert node["skill_id"] in {
                    skills["workspace-auditor"]["id"],
                    skills["file-ops"]["id"],
                }
        gate = next(n for n in rep["workflow"]["nodes"] if n["type"] == "hitl")
        assert {q["id"] for q in gate["questions"]} == {"fmt", "extra"}
        assert {s["name"] for s in rep["skills"]} == {"workspace-auditor", "file-ops"}

    async def test_static_rules_apply(self, seeded_client: AsyncClient) -> None:
        agents = (await seeded_client.get(f"{API}/sub-agents")).json()
        rep = next(a for a in agents if a["name"] == "workspace-reporter")
        resp = await seeded_client.patch(
            f"{API}/sub-agents/{rep['id']}", json={"persona": "hijacked"}
        )
        assert resp.status_code == 403
        resp = await seeded_client.patch(
            f"{API}/sub-agents/{rep['id']}", json={"direct_exposure": False}
        )
        assert resp.status_code == 200
        await seeded_client.patch(f"{API}/sub-agents/{rep['id']}", json={"direct_exposure": True})

    async def test_direct_invoke_through_form_gate(self, seeded_client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(
                session, {"default_model": "fake:scripted", "formatter_enabled": False}
            )
        agents = (await seeded_client.get(f"{API}/sub-agents")).json()
        rep = next(a for a in agents if a["name"] == "workspace-reporter")
        fake_llm.push_ai("AUDIT: 3 files, largest is notes.md (4 KB)")
        fake_llm.push_ai(  # router: workspace contains files → approve_report
            "", tool_calls=[{"name": "ConditionChoice", "args": {"index": 0}, "id": "r1"}]
        )
        resp = await seeded_client.post(
            f"{API}/sub-agents/{rep['id']}/invoke", json={"message": "document the workspace"}
        )
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["run_id"]
        run = await self._wait(seeded_client, run_id, {"paused_hitl", "failed"})
        assert run["status"] == "paused_hitl", run["error"]

        fake_llm.push_ai("WROTE: /workspace/workspace-audit-report.md")
        resp = await seeded_client.post(
            f"{API}/runs/{run_id}/hitl",
            json={
                "decision": "approve",
                "answers": {"fmt": "markdown", "extra": "include owner column"},
            },
        )
        assert resp.status_code == 200, resp.text
        run = await self._wait(seeded_client, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["orchestrator_mode"] == "direct"
        assert "WROTE:" in run["final_answer"]
        hitl = [s for s in run["steps"] if s["step_type"] == "hitl"]
        assert len(hitl) == 1

    @staticmethod
    async def _wait(
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


class TestReseedSemantics:
    async def test_lifecycle(self, seeded_client: AsyncClient, tmp_path: Path) -> None:
        skill = await create_skill(name=f"filed-{uuid4().hex[:4]}")
        name = f"filed-agent-{uuid4().hex[:4]}"
        f = tmp_path / f"{name}.agent.md"
        f.write_text(VALID_DOC.format(name=name, skill=skill.name))

        async def reseed() -> SubAgent | None:
            async with get_session_factory()() as session:
                await seed_agent_files(session, directory=tmp_path)
                from sqlalchemy import select

                return (
                    await session.execute(select(SubAgent).where(SubAgent.name == name))
                ).scalar_one_or_none()

        agent = await reseed()
        assert agent is not None and agent.status == "active"
        assert agent.direct_exposure is True
        agent_id = agent.id

        # user toggles OFF; file description changes → definition follows
        # file, toggles survive
        async with get_session_factory()() as session:
            row = await session.get(SubAgent, agent_id)
            assert row is not None
            row.direct_exposure = False
            row.status = "inactive"
            await session.commit()
        f.write_text(
            VALID_DOC.format(name=name, skill=skill.name).replace(
                "test agent from file", "updated description"
            )
        )
        agent = await reseed()
        assert agent is not None
        assert agent.description == "updated description"
        assert agent.direct_exposure is False
        assert agent.status == "inactive"

        # break the file (unknown skill) → status error; fix → active again
        async with get_session_factory()() as session:
            row = await session.get(SubAgent, agent_id)
            assert row is not None
            row.status = "active"
            await session.commit()
        f.write_text(VALID_DOC.format(name=name, skill="no-such-skill"))
        agent = await reseed()
        assert agent is not None and agent.status == "error"
        f.write_text(VALID_DOC.format(name=name, skill=skill.name))
        agent = await reseed()
        assert agent is not None and agent.status == "active"

        # remove the file → inactive, row kept
        f.unlink()
        agent = await reseed()
        assert agent is not None and agent.status == "inactive"

    async def test_malformed_file_skipped(self, seeded_client: AsyncClient, tmp_path: Path) -> None:
        (tmp_path / "broken.agent.md").write_text("no frontmatter at all")
        async with get_session_factory()() as session:
            await seed_agent_files(session, directory=tmp_path)  # must not raise
        agents = (await seeded_client.get(f"{API}/sub-agents")).json()
        assert all(a["native_ref"] != "file:broken.agent.md" for a in agents)
