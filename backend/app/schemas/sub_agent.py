"""Sub agent schemas (spec §3.4, §4)."""

from typing import Any, Literal

from app.schemas.common import ApiModel, RegistryOut, Status
from app.schemas.skill import SkillOut


class SubAgentCreate(ApiModel):
    name: str
    description: str = ""
    persona: str = ""
    model: str | None = None
    model_params: dict[str, Any] | None = None
    workflow: dict[str, Any]
    direct_exposure: bool = False


class SubAgentOverlapCheck(ApiModel):
    """Draft payload for the pre-save overlap judge (spec §4)."""

    name: str
    description: str = ""
    skill_ids: list[Any] = []
    exclude_id: Any | None = None


class SubAgentPatch(ApiModel):
    name: str | None = None
    description: str | None = None
    persona: str | None = None
    model: str | None = None
    model_params: dict[str, Any] | None = None
    workflow: dict[str, Any] | None = None
    status: Status | None = None
    direct_exposure: bool | None = None


class SubAgentOut(RegistryOut):
    kind: Literal["native", "custom"]
    persona: str
    model: str | None
    model_params: dict[str, Any] | None
    workflow: dict[str, Any] | None
    native_ref: str | None
    covers_skill_ids: list[Any] | None
    direct_exposure: bool
    skills: list[SkillOut] = []


class SubAgentInvoke(ApiModel):
    """Direct invocation body (spec §7.5)."""

    message: str
    conversation_id: Any | None = None
    # §7.5 opt-in: requires conversation_id (422 without — no history exists)
    include_history_summary: bool = False
    include_memories: bool = False


class ValidateResult(ApiModel):
    valid: bool
    errors: list[str] = []
