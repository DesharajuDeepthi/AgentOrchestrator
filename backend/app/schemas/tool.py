"""Tool schemas (spec §3.2, §4). Tools are never created via API — they come
from MCP ingestion or the native registration scan."""

from typing import Any, Literal
from uuid import UUID

from app.schemas.common import ApiModel, RegistryOut, Status


class ToolPatch(ApiModel):
    description: str | None = None
    status: Status | None = None
    direct_exposure: bool | None = None
    tool_key: str | None = None


class ToolOut(RegistryOut):
    kind: Literal["mcp", "native", "a2a"]
    mcp_server_id: UUID | None
    remote_agent_id: UUID | None
    tool_name: str
    native_ref: str | None
    tool_key: str
    direct_exposure: bool
    input_schema: dict[str, Any] | None
