"""remote_agents registry + a2a_tasks bookkeeping (spec §19.2).

RemoteAgent is a peer of McpServer: an external capability provider
registered by Agent Card URL, whose declared skills are projected into the
tools registry (kind='a2a'). `credentials` is WRITE-ONLY data — it must
never be serialized outward by any schema or log (spec §19.3).

A2ATask tracks one remote task per outbound call: created at send, adopted
on HITL resume replay via (run_id, call_key), parked when it outlives the
in-run budget (spec §19.5/§19.6). `state` holds the nine A2A states plus
the local 'parked'.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, RegistryRecord

# terminal remote states — everything else is in flight (spec §19.1)
A2A_TERMINAL_STATES = {"completed", "canceled", "failed", "rejected"}
A2A_OPEN_STATES = ("submitted", "working", "input-required", "auth-required", "parked")


class RemoteAgent(RegistryRecord):
    __tablename__ = "remote_agents"

    card_url: Mapped[str] = mapped_column(Text)
    card: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    card_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # {scheme_name: {"type": ..., "supported": bool}} — UI projection of the
    # card's securitySchemes; recomputed at every card (re)fetch
    auth_schemes: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # {scheme_name: str | {"client_id": ..., "client_secret": ...}} — values
    # may use the 'env:VAR_NAME' indirection; never serialized outward
    credentials: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class A2ATask(Base):
    __tablename__ = "a2a_tasks"
    __table_args__ = (Index("ix_a2a_tasks_agent_state", "remote_agent_id", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    remote_agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("remote_agents.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), default=None
    )
    # sha256 over (tool id, canonical args) — the HITL-resume adoption key
    call_key: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    remote_task_id: Mapped[str | None] = mapped_column(String(255), default=None)
    context_id: Mapped[str | None] = mapped_column(String(255), default=None)
    state: Mapped[str] = mapped_column(String(32), default="submitted")
    # the remote agent's input-required question (UNTRUSTED, fenced at render)
    question: Mapped[str | None] = mapped_column(Text, default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    parked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
