"""The kind='a2a' tool proxy (spec §19.5) — materialize_tool's third branch.

Lazy like the MCP proxy: the manager, agent row, and settings resolve at
CALL time, so a dead or dark backend is a tool-call error (error-edge
semantics) and recovery needs no rebuild. Call flow:

adopt-or-send → drain to a settle point under the `a2a_task_timeout_s`
budget → `input-required` raises the standard HITL interrupt (deny
cancels remotely; approve replies into the same remote task) → terminal
`completed` returns the FENCED result; other terminals raise fenced tool
errors. Budget expiry parks (ambient on, under the cap — spec §19.6) or
errors. Run cancellation propagates `tasks/cancel` best-effort.

Replay idempotency: HITL resume re-executes this coroutine; the open
`a2a_tasks` row for (run_id, call_key) is ADOPTED instead of re-sent,
and the replayed first `interrupt()` returns the human's decision — the
§7.1 spin_worker contract. GraphInterrupt is never caught here.
"""

import asyncio
import contextlib
from typing import Any
from uuid import UUID

import structlog
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt

from app.a2a import client_port, tasks
from app.a2a.client_port import RemoteOutcome
from app.a2a.fence import fence_remote_output, live_fence_cap
from app.db import get_session_factory
from app.models import RemoteAgent

logger = structlog.get_logger("a2a")

from app.models import A2A_TERMINAL_STATES  # noqa: E402

_SETTLED_STATES = A2A_TERMINAL_STATES | {"input-required", "auth-required"}


def _ops(kind: str, status: str) -> None:
    from app import obs

    obs.A2A_OPS.labels(kind=kind, status=status).inc()


async def _cancel_cleanup(agent_uuid: UUID, local_task_id: UUID) -> None:
    """Detached Stop-path cleanup: remote tasks/cancel + row bookkeeping."""
    with contextlib.suppress(Exception):
        row_now = await tasks.get_task(local_task_id)
        if row_now is not None and row_now.remote_task_id:
            await client_port.cancel_task(agent_uuid, row_now.remote_task_id)
        await tasks.update_task(local_task_id, state="canceled", error="run cancelled")
        _ops("cancel", "propagated")
        logger.info("a2a_cancel_propagated", tier="a2a", kind="cancel", task_id=str(local_task_id))


def make_a2a_proxy(row: dict[str, Any], sanitized_name: str) -> BaseTool:
    agent_id = row["remote_agent_id"]
    skill_id = row["tool_name"]
    tool_key = row["tool_key"]
    tool_id = str(row["id"])

    async def call(**kwargs: Any) -> Any:
        from app.registry_cache import get_cache

        cache = get_cache()
        if not bool(await cache.setting("a2a_enabled")):
            raise RuntimeError("a2a is disabled (a2a_enabled=false)")
        if agent_id is None:
            raise RuntimeError(f"tool {tool_key!r} has no remote agent")
        message = str(kwargs.get("message") or "").strip()
        data = kwargs.get("data") if isinstance(kwargs.get("data"), dict) else None
        if not message:
            raise RuntimeError("a2a tool requires a non-empty 'message'")
        agent_uuid = UUID(str(agent_id))
        async with get_session_factory()() as db:
            agent_row = await db.get(RemoteAgent, agent_uuid)
            if agent_row is None or agent_row.deleted_at is not None:
                raise RuntimeError(f"remote agent for {tool_key!r} is gone")
            agent_name = agent_row.name

        from app.orchestrator.context import get_run_context

        ctx = get_run_context()
        run_id = ctx.run_id if ctx is not None else None
        key = tasks.call_key_for(tool_id, {"message": message, "data": data})
        timeout_s = max(int(await cache.setting("a2a_task_timeout_s")), 1)

        open_task = await tasks.find_open_task(run_id, key)
        local_task_id: UUID | None = open_task.id if open_task is not None else None
        _ops("send", "adopted" if open_task is not None else "start")

        async def on_first_task(o: RemoteOutcome) -> None:
            nonlocal local_task_id
            local_task_id = await tasks.record_task(
                remote_agent_id=agent_uuid,
                run_id=run_id,
                call_key=key,
                remote_task_id=o.task_id,
                context_id=o.context_id,
                state=o.state,
                question=o.question,
            )

        async def _settle(first: RemoteOutcome) -> RemoteOutcome:
            """A send may legitimately end with a still-open task (polling
            transports, servers that detach execution) — poll tasks/get until
            a settle point; the surrounding asyncio.timeout owns the budget."""
            outcome = first
            while (  # noqa: ASYNC110 - polling a REMOTE server, no event exists
                outcome.state not in _SETTLED_STATES and outcome.task_id is not None
            ):
                await asyncio.sleep(1.0)
                outcome = await client_port.get_task_outcome(agent_uuid, outcome.task_id)
            return outcome

        try:
            if open_task is None:
                async with asyncio.timeout(timeout_s):
                    outcome = await _settle(
                        await client_port.send_text(
                            agent_uuid,
                            message,
                            data=data,
                            skill_id=skill_id,
                            on_task=on_first_task,
                        )
                    )
            elif open_task.remote_task_id:
                # replay adoption — recheck the live remote state, never re-send
                async with asyncio.timeout(timeout_s):
                    outcome = await _settle(
                        await client_port.get_task_outcome(agent_uuid, open_task.remote_task_id)
                    )
            else:
                outcome = RemoteOutcome(state=open_task.state, text="", question=open_task.question)

            while outcome.state == "input-required":
                if local_task_id is not None:
                    await tasks.update_task(
                        local_task_id, state="input-required", question=outcome.question
                    )
                _ops("hitl", "asked")
                # the standard HITL gate (spec §3.5 shape); the remote question
                # is human-facing here — the FENCE guards model contexts, the
                # provenance note guards the human
                decision = interrupt(
                    {
                        "prompt": (
                            f"Remote agent '{agent_name}' asks (untrusted, its own words): "
                            f"{(outcome.question or '').strip() or '(no question text)'}"
                        ),
                        "node_id": f"a2a:{tool_key}",
                        "questions": [
                            {
                                "id": "reply",
                                "kind": "text",
                                "prompt": "Reply to the remote agent",
                            }
                        ],
                    }
                )
                answer = decision if isinstance(decision, dict) else {}
                if answer.get("decision") == "deny":
                    if outcome.task_id:
                        with contextlib.suppress(Exception):
                            await client_port.cancel_task(agent_uuid, outcome.task_id)
                    if local_task_id is not None:
                        await tasks.update_task(
                            local_task_id, state="canceled", error="denied by human reviewer"
                        )
                    _ops("hitl", "denied")
                    note = str(answer.get("note") or "").strip()
                    raise RuntimeError(
                        "remote agent task denied by human reviewer" + (f": {note}" if note else "")
                    )
                reply = str(
                    (answer.get("answers") or {}).get("reply")
                    or answer.get("note")
                    or "approved — proceed"
                )
                _ops("hitl", "replied")
                async with asyncio.timeout(timeout_s):
                    outcome = await _settle(
                        await client_port.send_text(
                            agent_uuid,
                            reply,
                            task_id=outcome.task_id,
                            context_id=outcome.context_id,
                            skill_id=skill_id,
                        )
                    )

            fence_cap = await live_fence_cap()
            if outcome.state == "completed":
                if local_task_id is not None:
                    await tasks.update_task(
                        local_task_id, state="completed", result_text=outcome.text
                    )
                _ops("send", "completed")
                return fence_remote_output(outcome.text, agent_name=agent_name, max_chars=fence_cap)

            error_text = outcome.error or outcome.text or f"task ended {outcome.state}"
            if local_task_id is not None:
                await tasks.update_task(local_task_id, state=outcome.state, error=error_text)
            _ops("send", outcome.state)
            raise RuntimeError(
                f"remote agent task {outcome.state}: "
                + fence_remote_output(
                    error_text, agent_name=agent_name, state=outcome.state, max_chars=fence_cap
                )
            )

        except TimeoutError:
            # budget expiry: park under ambient (spec §19.6) or plain error
            if run_id is not None and local_task_id is not None:
                ambient_on = bool(await cache.setting("ambient_enabled"))
                cap = int(await cache.setting("a2a_max_parked"))
                if ambient_on and await tasks.parked_count() < cap:
                    await tasks.update_task(local_task_id, parked=True)
                    _ops("park", "parked")
                    logger.info(
                        "a2a_task_parked",
                        tier="a2a",
                        kind="park",
                        task_id=str(local_task_id),
                        agent_id=str(agent_uuid),
                    )
                    return (
                        f"The remote agent '{agent_name}' is still working after "
                        f"{timeout_s}s. The task was parked; its result will be "
                        "delivered ambiently when it finishes — do not wait for it."
                    )
            if local_task_id is not None:
                await tasks.update_task(
                    local_task_id, state="unknown", error=f"timed out after {timeout_s}s"
                )
            _ops("send", "timeout")
            raise RuntimeError(
                f"remote agent call timed out after {timeout_s}s "
                "(a2a_task_timeout_s; parking needs ambient_enabled)"
            ) from None

        except asyncio.CancelledError:
            # run Stop — propagate tasks/cancel best-effort (spec §19.5).
            # The proxy's task is being torn down, so cleanup runs DETACHED:
            # awaiting network/DB work inside a cancelling task is unreliable
            # and must never mask the cancellation itself.
            if local_task_id is not None:
                cleanup = asyncio.create_task(_cancel_cleanup(agent_uuid, local_task_id))
                cleanup.add_done_callback(lambda t: t.exception())
            raise

    return StructuredTool(
        name=sanitized_name,
        description=row.get("description") or f"Remote A2A agent skill {tool_key}",
        args_schema=row.get("input_schema")
        or {"type": "object", "properties": {"message": {"type": "string"}}},
        coroutine=call,
    )
