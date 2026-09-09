"""Fake-provider script control (demo/testing only).

Mounted 404-invisible unless FAKE_LLM_ENABLED is set: lets an external
driver (the acceptance walk, keyless compose demos) queue deterministic
fake-model responses exactly like the in-process pytest suites do
(spec §11). Never active in a normally configured deployment.
"""

from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import get_config
from app.llm import fake as fake_llm
from app.schemas.common import ApiModel

router = APIRouter(prefix="/_fake", tags=["fake-llm"])


class FakeCall(ApiModel):
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    error: str | None = None
    delay_s: float | None = None


class FakeScript(ApiModel):
    calls: list[FakeCall]


def _ensure_enabled() -> None:
    if not get_config().fake_llm_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.post("/script")
async def push_script(body: FakeScript) -> dict[str, Any]:
    _ensure_enabled()
    for call in body.calls:
        if call.error:
            fake_llm.push_error(RuntimeError(call.error))
        else:
            fake_llm.push_ai(call.content, call.tool_calls, delay_s=call.delay_s)
    return {"queued": len(body.calls), "pending": fake_llm.script_len()}


@router.post("/clear")
async def clear_script() -> dict[str, Any]:
    _ensure_enabled()
    fake_llm.clear_script()
    return {"pending": 0}
