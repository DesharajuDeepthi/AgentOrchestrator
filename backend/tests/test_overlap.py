"""Overlap guard (spec §4): LLM-as-judge duplicate detection at save time.

The check endpoints are advisory — they flag ≥70% overlaps for the UI's
confirm/cancel dialog, exclude the record being updated, and fail open when
the judge is unavailable. Tools are exempt by design (dynamic MCP ingest)."""

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.settings_store import update_settings
from tests.factory_helpers import create_skill, create_sub_agent, create_tool

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _judge_settings() -> None:
    async with get_session_factory()() as session:
        await update_settings(session, {"default_model": "fake:scripted"})
    fake_llm.clear_script()


def push_verdict(percent: int, match_type: str = "skill", name: str | None = None) -> None:
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "OverlapVerdict",
                "args": {
                    "overlap_percent": percent,
                    "match_type": match_type if percent else "none",
                    "match_id": None,
                    "match_name": name,
                    "reasoning": "scripted verdict",
                },
                "id": f"ov{uuid4().hex[:6]}",
            }
        ],
    )


class TestSkillOverlap:
    async def test_flags_at_threshold(self, client: AsyncClient) -> None:
        existing = await create_skill(name=f"summarizer-{uuid4().hex[:4]}")
        push_verdict(82, "skill", existing.name)
        resp = await client.post(
            f"{API}/skills/check-overlap",
            json={"name": "another summarizer", "description": "summarizes files"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["overlap"] is True
        assert body["threshold"] == 70
        assert body["overlap_percent"] == 82
        assert body["match_name"] == existing.name

    async def test_below_threshold_passes(self, client: AsyncClient) -> None:
        await create_skill(name=f"unrelated-{uuid4().hex[:4]}")
        push_verdict(35)
        resp = await client.post(f"{API}/skills/check-overlap", json={"name": "totally new thing"})
        assert resp.json()["overlap"] is False

    async def test_update_excludes_self(self, client: AsyncClient) -> None:
        """Editing a record must not be flagged as overlapping itself: the
        prompt's candidate list omits the excluded id entirely."""
        skill = await create_skill(name=f"self-{uuid4().hex[:4]}")
        fake_llm.clear_seen_tools()
        push_verdict(0)
        resp = await client.post(
            f"{API}/skills/check-overlap",
            json={"name": skill.name, "exclude_id": str(skill.id)},
        )
        assert resp.status_code == 200
        # the fake records the bound OverlapVerdict schema; the judge ran —
        # now prove the excluded skill was not among the candidates by
        # scripting a verdict naming it and checking the endpoint stays sane
        # (structural check: exclusion happens before the prompt is built)
        from app.overlap import _skill_candidates

        lines = await _skill_candidates(skill.id)
        assert not any(str(skill.id) in line for line in lines)

    async def test_judge_error_fails_open(self, client: AsyncClient) -> None:
        await create_skill(name=f"whatever-{uuid4().hex[:4]}")
        fake_llm.push_error(RuntimeError("provider exploded"))
        resp = await client.post(f"{API}/skills/check-overlap", json={"name": "draft"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["overlap"] is False
        assert "judge unavailable" in body["reasoning"]

    async def test_tool_can_be_the_match(self, client: AsyncClient) -> None:
        tool = await create_tool(tool_name="orch_echo", tool_key=f"echo-{uuid4().hex[:4]}")
        push_verdict(91, "tool", tool.tool_key)
        resp = await client.post(
            f"{API}/skills/check-overlap",
            json={"name": "echo wrapper", "tool_ids": [str(tool.id)]},
        )
        body = resp.json()
        assert body["overlap"] is True and body["match_type"] == "tool"


class TestSubAgentOverlap:
    async def test_flags_sub_agent_match(self, client: AsyncClient) -> None:
        s = await create_skill(name=f"sa-skill-{uuid4().hex[:4]}")
        existing = await create_sub_agent(
            {
                "nodes": [{"id": "work", "type": "skill", "skill_id": str(s.id)}],
                "edges": [{"from": "START", "to": "work"}, {"from": "work", "to": "END"}],
            },
            name=f"analyst-{uuid4().hex[:4]}",
        )
        push_verdict(78, "sub_agent", existing.name)
        resp = await client.post(
            f"{API}/sub-agents/check-overlap",
            json={
                "name": "another analyst",
                "description": "does the same analysis",
                "skill_ids": [str(s.id)],
            },
        )
        body = resp.json()
        assert body["overlap"] is True
        assert body["match_type"] == "sub_agent"
        assert body["match_name"] == existing.name

    async def test_no_candidates_short_circuits(self, client: AsyncClient) -> None:
        """With nothing to compare against, no model call is made at all."""
        from app.overlap import _judge

        before = fake_llm.script_len()
        verdict = await _judge("skill", "draft", [])
        assert verdict.overlap_percent == 0
        assert fake_llm.script_len() == before
