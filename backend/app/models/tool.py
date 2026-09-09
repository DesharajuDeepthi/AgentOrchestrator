"""tools registry (spec §3.2)."""

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import RegistryRecord


class Tool(RegistryRecord):
    __tablename__ = "tools"

    kind: Mapped[str] = mapped_column(String(16))  # 'mcp' | 'native' | 'a2a'
    mcp_server_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcp_servers.id"), default=None
    )
    remote_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("remote_agents.id"), default=None
    )
    tool_name: Mapped[str] = mapped_column(String(255))
    native_ref: Mapped[str | None] = mapped_column(String(512), default=None)
    tool_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    direct_exposure: Mapped[bool] = mapped_column(Boolean, default=False)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # retrieval vector (spec §7.4): maintained best-effort on the write path
    embedding: Mapped[list[Any] | None] = mapped_column(default=None)
    embedding_hash: Mapped[str | None] = mapped_column(String(64), default=None)
