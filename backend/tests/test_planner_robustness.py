"""Planner repair loop covers OutputParserException (spec §7.1): a
thinking-enabled model that answers in prose instead of calling the forced
plan tool gets one repair attempt before the run fails."""

from typing import Any

import pytest
from langchain_core.exceptions import OutputParserException

from app.db import get_session_factory
from app.orchestrator.planner import PlanFailure, PlannerOutput, run_planner


class _FlakyStructured:
    """Raises on the first ainvoke (prose-instead-of-tool-call), succeeds on
    the repair attempt."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def ainvoke(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OutputParserException("no tool call in response (thinking-only output)")
        return {
            "raw": None,
            "parsed": PlannerOutput(direct_answer="recovered on repair"),
            "parsing_error": None,
        }


class _FakeModel:
    def __init__(self, fail_times: int) -> None:
        self.structured = _FlakyStructured(fail_times)

    def with_structured_output(self, *_a: Any, **_k: Any) -> _FlakyStructured:
        return self.structured


async def test_parser_exception_is_repaired_once() -> None:
    model = _FakeModel(fail_times=1)
    async with get_session_factory()() as session:
        plan, raw_outputs, _usage = await run_planner(session, model, "hi", "", 6)  # type: ignore[arg-type]
    assert plan.direct_answer == "recovered on repair"
    assert model.structured.calls == 2
    assert any("OutputParserException" in str(r) for r in raw_outputs)


async def test_parser_exception_twice_fails_cleanly() -> None:
    model = _FakeModel(fail_times=2)
    async with get_session_factory()() as session:
        with pytest.raises(PlanFailure) as exc_info:
            await run_planner(session, model, "hi", "", 6)  # type: ignore[arg-type]
    assert "schema validation" in str(
        exc_info.value.errors[0] if hasattr(exc_info.value, "errors") else exc_info.value
    )
