"""Agentic-mode orchestrator (spec §7.2): a single create_agent concierge.

Planning is emergent (TodoListMiddleware — the SSE `plan` events carry the
live todo list); capabilities at every model call are exactly the live
registries via the three registry middlewares; spin_worker covers rung 4 and
use_full_catalog is the logged fallback escalation.
"""

from typing import Any

import structlog
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import StructuredTool

from app.db import get_checkpointer, get_session_factory
from app.factory.worker import _usage_from_messages, resolve_node_model
from app.llm import get_model, text_from_content
from app.models import Run
from app.orchestrator.context import require_run_context
from app.orchestrator.ladder import ResolutionError, execute_resolution, resolve_capability
from app.orchestrator.middleware import AgenticLoopContext, build_middleware_stack
from app.prompts import load_prompt
from app.settings_store import get_setting

logger = structlog.get_logger("orchestrator.agentic")


def _spin_worker_tool() -> StructuredTool:
    async def spin_worker(skill_ids: list[str], task: str) -> str:
        """Build a one-off ephemeral worker over registry skills (rung-4
        fallback) and run it on the task. skill_ids MUST be registry skill
        ids (uuids) exactly as shown in the Available skills catalog — never
        skill names; if a skill is not in the catalog, call use_full_catalog
        first to see the full registry."""
        ctx = require_run_context()
        try:
            resolution = await resolve_capability({"type": "spin_worker", "skill_ids": skill_ids})
        except ResolutionError as exc:
            # bad ids from the model must not kill the run — surface the
            # problem as the tool result so the loop can self-correct
            return f"could not spin a worker: {exc}"
        await ctx.recorder.record_route(
            capability={"type": "spin_worker", "skill_ids": skill_ids},
            rung=resolution.rung,
            resolved_to=resolution.as_route(),
            kind="dynamic",
            source="dynamic",
        )
        # unique node id per spin: parallel workers must not share the
        # checkpoint thread ("agentic:spin_worker" would collide)
        node_id = f"agentic:{resolution.payload.get('callsign', 'spin_worker')}"
        result = await execute_resolution(resolution, task, node_id)
        if result.get("status") != "ok":
            return f"worker failed: {result.get('error')}"
        return str(result.get("output", ""))

    return StructuredTool.from_function(coroutine=spin_worker, name="spin_worker")


def _use_full_catalog_tool() -> StructuredTool:
    async def use_full_catalog() -> str:
        """Unlock every active tool and skill in the registry for the rest of
        this run (fallback escalation — use only when the exposed selection
        cannot cover the request)."""
        ctx = require_run_context()
        ctx.flags.full_catalog = True
        await ctx.recorder.record_route(
            capability={"type": "full_catalog"},
            rung="fallback",
            resolved_to={"entity_id": None, "entity_name": "full-catalog escalation"},
            kind="orchestrator",
        )
        return (
            "Full catalog unlocked: every active tool and skill is now available on your next step."
        )

    return StructuredTool.from_function(coroutine=use_full_catalog, name="use_full_catalog")


async def build_agentic_agent() -> Any:
    model_ref, params = await resolve_node_model({}, {})
    model = get_model(model_ref, params)
    async with get_session_factory()() as session:
        max_iter = int(await get_setting(session, "max_tool_iterations"))
    from langchain.agents import create_agent

    stack = build_middleware_stack(AgenticLoopContext(model=model, max_tool_iterations=max_iter))
    checkpointer = await get_checkpointer()
    # spec §16.3: the concierge system prompt carries the remembered-context
    # block for this run's task — fail-open, empty when memory is off
    from app.memory.inject import build_memory_block
    from app.orchestrator.context import get_run_context

    run_ctx = get_run_context()
    memory_block, _ = await build_memory_block(
        run_ctx.query_text if run_ctx else "",
        conversation_id=run_ctx.conversation_id if run_ctx else None,
        surface="agentic",
    )
    system_prompt = load_prompt("concierge") + (f"\n\n{memory_block}" if memory_block else "")
    return create_agent(
        model,
        tools=[_spin_worker_tool(), _use_full_catalog_tool()],
        system_prompt=system_prompt,
        middleware=stack,
        checkpointer=checkpointer,
    )


async def build_history_messages(conversation_id: Any) -> list[BaseMessage]:
    from langchain_core.messages import AIMessage
    from sqlalchemy import select

    async with get_session_factory()() as session:
        runs = list(
            (
                await session.execute(
                    select(Run)
                    .where(Run.conversation_id == conversation_id, Run.status == "completed")
                    .order_by(Run.started_at)
                )
            ).scalars()
        )
    messages: list[BaseMessage] = []
    for run in runs[-10:]:
        messages.append(HumanMessage(content=run.chat_message))
        if run.final_answer:
            messages.append(AIMessage(content=run.final_answer))
    return messages


def extract_todos(state_values: dict[str, Any]) -> list[dict[str, Any]] | None:
    todos = state_values.get("todos")
    if isinstance(todos, list):
        return [
            {"content": t.get("content"), "status": t.get("status")}
            for t in todos
            if isinstance(t, dict)
        ]
    return None


def final_answer_from_messages(messages: list[Any]) -> str:
    for message in reversed(messages):
        if message.type == "ai" and not getattr(message, "tool_calls", None):
            return text_from_content(message.content)
    return ""


def usage_from_state(messages: list[Any]) -> dict[str, int]:
    return _usage_from_messages(messages)
