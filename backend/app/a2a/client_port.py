"""The call-path port over the SDK client (spec §19.5).

One consumption shape for streaming and polling counterparties: the SDK
``Client.send_message`` iterator is drained into a ``RemoteOutcome`` —
the only surface the M38 proxy (and tests) touch. All strings inside the
outcome are raw remote output: callers MUST fence them (app.a2a.fence)
before they reach any model context."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from a2a.types import Message, Part, Role, Task, TextPart

from app.models import A2A_TERMINAL_STATES

# states after which the send iterator will yield nothing further useful
_STOP_STATES = A2A_TERMINAL_STATES | {"input-required", "auth-required"}


@dataclass
class RemoteOutcome:
    state: str  # last observed A2A state ('working' if the stream ended early)
    text: str  # concatenated artifact + status-message text (UNTRUSTED)
    task_id: str | None = None
    context_id: str | None = None
    question: str | None = None  # set when state == 'input-required'
    error: str | None = None


def _parts_text(parts: list[Part] | None) -> str:
    out: list[str] = []
    for part in parts or []:
        root = part.root
        if isinstance(root, TextPart) and root.text:
            out.append(root.text)
    return "\n".join(out)


def _task_text(task: Task) -> str:
    chunks: list[str] = []
    for artifact in task.artifacts or []:
        text = _parts_text(artifact.parts)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def outcome_from_task(task: Task) -> RemoteOutcome:
    state = task.status.state.value if task.status else "unknown"
    status_text = (
        _parts_text(task.status.message.parts) if task.status and task.status.message else ""
    )
    text = _task_text(task)
    return RemoteOutcome(
        state=state,
        text=text or ("" if state == "input-required" else status_text),
        task_id=task.id,
        context_id=task.context_id,
        question=status_text if state == "input-required" else None,
        error=status_text if state in {"failed", "rejected"} and status_text else None,
    )


def build_message(
    text: str,
    data: dict[str, Any] | None = None,
    task_id: str | None = None,
    context_id: str | None = None,
    skill_id: str | None = None,
) -> Message:
    parts: list[Part] = [Part(root=TextPart(text=text))]
    if data:
        from a2a.types import DataPart

        parts.append(Part(root=DataPart(data=data)))
    return Message(
        message_id=uuid4().hex,
        role=Role.user,
        parts=parts,
        task_id=task_id,
        context_id=context_id,
        metadata={"skill_id": skill_id} if skill_id else None,
    )


async def send_message(
    agent_id: UUID,
    message: Message,
    on_task: "Callable[[RemoteOutcome], Awaitable[None]] | None" = None,
) -> RemoteOutcome:
    """Send and drain to the first settle point (terminal / input-required /
    stream end). Long-poll budgeting is the CALLER's job (asyncio.timeout) —
    this port never sleeps on its own. ``on_task`` fires once at the first
    task observation so a caller whose budget expires mid-stream still knows
    the remote task id (park path, spec §19.6)."""
    from app.a2a.manager import get_manager

    manager = get_manager()
    if manager is None:
        raise RuntimeError("A2A manager not running")
    client, _card = await manager.build_client(agent_id)
    last: RemoteOutcome | None = None
    observed = False
    async for event in client.send_message(message):
        if isinstance(event, Message):
            # message-only reply — treat as an immediate completion
            return RemoteOutcome(
                state="completed",
                text=_parts_text(event.parts),
                task_id=event.task_id,
                context_id=event.context_id,
            )
        task, _update = event
        last = outcome_from_task(task)
        if not observed and on_task is not None:
            observed = True
            await on_task(last)
        if last.state in _STOP_STATES:
            break
    return last or RemoteOutcome(state="unknown", text="", error="no response events")


async def send_text(
    agent_id: UUID,
    text: str,
    *,
    data: dict[str, Any] | None = None,
    task_id: str | None = None,
    context_id: str | None = None,
    skill_id: str | None = None,
    on_task: "Callable[[RemoteOutcome], Awaitable[None]] | None" = None,
) -> RemoteOutcome:
    return await send_message(
        agent_id,
        build_message(text, data=data, task_id=task_id, context_id=context_id, skill_id=skill_id),
        on_task=on_task,
    )


async def get_task_outcome(agent_id: UUID, remote_task_id: str) -> RemoteOutcome:
    """Recheck a known task (parked-task poller + adoption path)."""
    from a2a.types import TaskQueryParams

    from app.a2a.manager import get_manager

    manager = get_manager()
    if manager is None:
        raise RuntimeError("A2A manager not running")
    client, _card = await manager.build_client(agent_id)
    task = await client.get_task(TaskQueryParams(id=remote_task_id))
    return outcome_from_task(task)


async def cancel_task(agent_id: UUID, remote_task_id: str) -> None:
    """Best-effort remote cancellation (Stop propagation, spec §19.5)."""
    from a2a.types import TaskIdParams

    from app.a2a.manager import get_manager

    manager = get_manager()
    if manager is None:
        return
    client, _card = await manager.build_client(agent_id)
    await client.cancel_task(TaskIdParams(id=remote_task_id))


__all__ = [
    "RemoteOutcome",
    "build_message",
    "cancel_task",
    "get_task_outcome",
    "outcome_from_task",
    "send_message",
    "send_text",
]
