"""Native tool provider (spec §5b): registration scan, schema derivation,
guardrail rejections, mixed mcp+native skill invocation, subgraph tool."""

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.factory.worker import build_worker, resolve_skill_tools, snapshot_skill
from app.llm import fake as fake_llm
from app.native.provider import (
    NativeGuardrailError,
    derive_input_schema,
    native_tool,
    native_tools,
    scan_native,
)
from tests.factory_helpers import create_skill, create_sub_agent, create_tool

STUB = str(Path(__file__).resolve().parent / "stub_mcp_server.py")


@pytest.fixture(autouse=True)
async def _fake_default_model() -> None:
    from app.db import get_session_factory
    from app.settings_store import update_settings

    async with get_session_factory()() as session:
        await update_settings(session, {"default_model": "fake:scripted"})


class TestRegistrationScan:
    def test_summarize_and_structure_registered(self) -> None:
        scan_native()
        entries = native_tools()
        assert "summarize-and-structure" in entries
        entry = entries["summarize-and-structure"]
        assert entry.native_ref.startswith("app.native.tools.")
        assert "text" in entry.input_schema["properties"]

    def test_schema_derivation_from_signature(self) -> None:
        async def sample(query: str, limit: int = 5) -> str:
            return query

        schema = derive_input_schema(sample)
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["properties"]["limit"]["default"] == 5
        assert "query" in schema["required"]


class TestGuardrails:
    def test_interrupt_in_subgraph_rejected(self) -> None:
        with pytest.raises(NativeGuardrailError, match="interrupt"):

            @native_tool("bad-hitl-tool", "contains an interrupt")
            async def bad_hitl(text: str) -> str:
                from langgraph.types import interrupt

                return str(interrupt({"prompt": text}))

    def test_sub_agent_wrap_rejected(self) -> None:
        with pytest.raises(NativeGuardrailError, match="sub agent"):

            @native_tool("bad-wrap-tool", "wraps a registry sub agent")
            async def bad_wrap(text: str) -> str:
                from app.factory.worker import build_worker

                worker = build_worker({}, checkpointer=None)
                return str(worker)


class TestMixedSkillInvocation:
    async def test_skill_resolves_mcp_and_native_tools(self, manager: Any) -> None:
        from tests.test_mcp_manager import make_stub_server

        server_id = await make_stub_server(name="mix-stub")
        await manager.connect_server(server_id)

        from sqlalchemy import select

        from app.db import get_session_factory
        from app.models import Tool

        async with get_session_factory()() as session:
            echo = (
                await session.execute(
                    select(Tool).where(Tool.mcp_server_id == server_id, Tool.tool_name == "echo")
                )
            ).scalar_one()
            native = await create_tool(
                tool_name="summarize-and-structure",
                tool_key=f"sas-{echo.id.hex[:6]}",
                kind="native",
                native_ref="app.native.tools.summarize_and_structure",
            )
            skill = await create_skill(name="mixed", tools=[echo, native])
            snap = await snapshot_skill(session, await session.merge(skill))

        tools = await resolve_skill_tools(snap)
        names = sorted(t.name for t in tools)
        assert len(tools) == 2
        assert any("echo" in n for n in names)
        # the mcp one actually invokes through the live session
        mcp_tool = next(t for t in tools if "echo" in t.name)
        result = await mcp_tool.ainvoke({"text": "ping"})
        assert "echo:ping" in str(result)

    async def test_mcp_tool_error_fails_node(self, manager: Any) -> None:
        """Tool/MCP error inside a skill loop → node failure (spec §3.5)."""
        from tests.test_mcp_manager import make_stub_server

        server_id = await make_stub_server(name="err-stub")
        await manager.connect_server(server_id)
        from sqlalchemy import select

        from app.db import get_session_factory
        from app.factory.worker import NodeExecutionError
        from app.models import Tool

        async with get_session_factory()() as session:
            echo = (
                await session.execute(
                    select(Tool).where(Tool.mcp_server_id == server_id, Tool.tool_name == "echo")
                )
            ).scalar_one()
        skill = await create_skill(name="mcp-err", tools=[echo])
        agent = await create_sub_agent(
            {
                "nodes": [{"id": "n", "type": "skill", "skill_id": str(skill.id)}],
                "edges": [{"from": "START", "to": "n"}, {"from": "n", "to": "END"}],
            }
        )
        from tests.factory_helpers import load_snapshot

        worker = build_worker(await load_snapshot(agent.id), checkpointer=MemorySaver())
        # kill the server so the tool call fails mid-run
        await manager.disconnect_server(server_id)
        from app.factory.worker import sanitize_tool_name

        fake_llm.push_ai(
            "",
            tool_calls=[
                {"name": sanitize_tool_name(echo.tool_key), "args": {"text": "x"}, "id": "t"}
            ],
        )
        with pytest.raises(NodeExecutionError):
            await worker.ainvoke(
                {"task": "use the dead tool", "messages": []},
                config={"configurable": {"thread_id": "dead-tool"}},
            )


@pytest.fixture
async def manager() -> Any:
    from app.mcp.manager import McpManager, set_manager

    m = McpManager()
    set_manager(m)
    yield m
    set_manager(None)
    await m.stop()


class TestSubgraphTool:
    async def test_summarize_subgraph_runs_via_port(self) -> None:
        from app.db import get_session_factory
        from app.native.tools import summarize_and_structure
        from app.settings_store import update_settings

        async with get_session_factory()() as session:
            await update_settings(session, {"default_model": "fake:scripted"})
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": "StructuredSummary",
                    "args": {
                        "title": "T",
                        "summary": "S",
                        "key_points": ["k1"],
                        "entities": ["e1"],
                    },
                    "id": "s1",
                }
            ],
        )
        result = await summarize_and_structure("raw text to summarize")
        assert result["title"] == "T"
        assert result["key_points"] == ["k1"]


class TestSummarizeRepairRetry:
    """Structured-output schema violations get one repair retry with the
    validation errors fed back (same strict+repair pattern as the planner)."""

    async def test_invalid_first_attempt_then_repair_succeeds(self) -> None:
        from app.db import get_session_factory
        from app.llm import fake as fake_llm
        from app.native.tools import summarize_and_structure
        from app.settings_store import update_settings

        async with get_session_factory()() as session:
            await update_settings(session, {"default_model": "fake:scripted"})
        fake_llm.push_error(ValueError("key_points: Input should be a valid list"))
        fake_llm.push_ai(
            "",
            tool_calls=[
                {
                    "name": "StructuredSummary",
                    "args": {
                        "title": "T",
                        "summary": "S.",
                        "key_points": ["a", "b", "c"],
                        "entities": ["X"],
                    },
                    "id": "sas-repair",
                }
            ],
        )
        out = await summarize_and_structure("raw text to summarize")
        assert out["key_points"] == ["a", "b", "c"]
        assert out["entities"] == ["X"]
