"""mcp_servers registry (spec §3.1)."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import RegistryRecord


class McpServer(RegistryRecord):
    __tablename__ = "mcp_servers"

    transport: Mapped[str] = mapped_column(String(16))  # 'stdio' | 'http'
    command: Mapped[str | None] = mapped_column(Text, default=None)
    args: Mapped[list[Any] | None] = mapped_column(default=None)
    env: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    url: Mapped[str | None] = mapped_column(Text, default=None)
    headers: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
