"""Eval graders (spec §15): exact | contains | llm_judge. The judge is ONE
structured call on the extraction-model role with judge_notes as grading
guidance; judge failure grades the case `error` — never a silent pass."""

from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger("evals")


class EvalVerdict(BaseModel):
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str


def _normalize(text: str) -> str:
    return " ".join(text.split()).strip().lower()


async def grade_case(
    *, grader: str, answer: str, expected: str, judge_notes: str
) -> dict[str, Any]:
    """Returns {status: graded|error, passed, score, reason}."""
    if grader == "exact":
        passed = _normalize(answer) == _normalize(expected)
        return {
            "status": "graded",
            "passed": passed,
            "score": 1.0 if passed else 0.0,
            "reason": "exact match" if passed else "normalized strings differ",
        }
    if grader == "contains":
        passed = _normalize(expected) in _normalize(answer)
        return {
            "status": "graded",
            "passed": passed,
            "score": 1.0 if passed else 0.0,
            "reason": ("expected substring present" if passed else "expected substring not found"),
        }
    # llm_judge
    try:
        from app.memory.extract import _extraction_model
        from app.prompts import load_prompt

        prompt = load_prompt("eval_judge").format(
            input_hint="",
            expected=expected[:4000] or "(none given)",
            judge_notes=judge_notes[:2000] or "(none)",
            answer=answer[:8000],
        )
        _, model = await _extraction_model()
        out = await model.with_structured_output(EvalVerdict).ainvoke(prompt)  # type: ignore[attr-defined]
        assert isinstance(out, EvalVerdict)
        return {
            "status": "graded",
            "passed": out.passed,
            "score": round(out.score, 4),
            "reason": out.reason[:2000],
        }
    except Exception as exc:  # noqa: BLE001 — judge failure ⇒ error, never a pass
        logger.warning("eval_judge_failed", tier="evals", kind="grade", error=str(exc))
        return {
            "status": "error",
            "passed": False,
            "score": 0.0,
            "reason": f"judge failed: {exc}"[:2000],
        }
