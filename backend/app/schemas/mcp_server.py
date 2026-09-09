"""MCP server schemas (spec §3.1, §4)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import model_validator

from app.schemas.common import ApiModel, RegistryOut, Status

Transport = Literal["stdio", "http"]


class McpServerCreate(ApiModel):
    name: str
    description: str = ""
    transport: Transport
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def check_transport_fields(self) -> "McpServerCreate":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport == "http" and not self.url:
            raise ValueError("http transport requires 'url'")
        return self


class McpServerPatch(ApiModel):
    name: str | None = None
    description: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    status: Status | None = None


class McpServerOut(RegistryOut):
    transport: Transport
    command: str | None
    args: list[Any] | None
    env: dict[str, Any] | None
    url: str | None
    headers: dict[str, Any] | None
    last_connected_at: datetime | None
    last_error: str | None
    tool_count: int = 0
