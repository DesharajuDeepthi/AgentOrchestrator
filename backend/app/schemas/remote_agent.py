"""Remote agent schemas (spec §19.2/§19.3, §4).

`credentials` is WRITE-ONLY: it appears in Create/Patch bodies and is
never present on any Out model — the UI sees per-scheme configured flags
only (`auth` on RemoteAgentOut)."""

from datetime import datetime
from typing import Any

from app.schemas.common import ApiModel, RegistryOut


class RemoteAgentCreate(ApiModel):
    card_url: str
    name: str | None = None  # defaults to the card's declared name
    description: str = ""
    # {scheme_name: "secret" | "env:VAR" | {"client_id": ..., "client_secret": ...}}
    credentials: dict[str, Any] | None = None


class RemoteAgentPatch(ApiModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    # merge semantics: {scheme: value} sets, {scheme: None} clears
    credentials: dict[str, Any] | None = None


class RemoteAgentOut(RegistryOut):
    card_url: str
    card: dict[str, Any] | None
    card_fetched_at: datetime | None
    last_error: str | None
    tool_count: int = 0
    # {scheme_name: {"type": ..., "supported": bool, "configured": bool}}
    auth: dict[str, Any] = {}
    # 'ok' | 'unconfigured' | 'unsupported' | 'open' (card declares no auth)
    auth_status: str = "open"


class A2ATaskOut(ApiModel):
    id: str
    remote_agent_id: str
    run_id: str | None
    remote_task_id: str | None
    state: str
    question: str | None
    error: str | None
    parked_at: datetime | None
    delivered: bool
    created_at: datetime
    updated_at: datetime
