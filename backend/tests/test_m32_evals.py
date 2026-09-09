"""M32 — evals (spec §15): upload parsing, the three graders, the
admin-direct runner (rung-4 exposure exempt), eval=true tagging, and the
results storage chain."""

import io
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.db import get_session_factory
from app.evals.grade import grade_case
from app.evals.parse import EvalParseError, parse_eval_file
from app.models import EvalResult, Run
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio

CSV = (
    "level,target_id,input,expected,judge_notes,grader\n"
    'skill,{tid},"what is 2+2?","4","",exact\n'
    'skill,{tid},"name a primary color","red","any of red/blue/yellow passes",llm_judge\n'
)


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _seeded_skill(client: Any, name: str = "web-research") -> dict[str, Any]:
    resp = await client.get("/api/v1/skills")
    return next(s for s in resp.json() if s["name"] == name)


# ── upload parsing (spec §15 format) ─────────────────────────────────


def test_parse_csv_happy_path() -> None:
    tid = str(uuid4())
    rows = parse_eval_file("cases.csv", CSV.format(tid=tid).encode())
    assert rows["level"] == "skill" and rows["target_id"] == tid
    assert len(rows["cases"]) == 2
    assert rows["cases"][0]["grader"] == "exact"
    assert rows["cases"][1]["judge_notes"].startswith("any of")


def test_parse_rejects_mixed_targets_and_bad_grader() -> None:
    a, b = str(uuid4()), str(uuid4())
    mixed = f"level,target_id,input,expected\nskill,{a},q1,a1\nskill,{b},q2,a2\n"
    with pytest.raises(EvalParseError, match="single target"):
        parse_eval_file("cases.csv", mixed.encode())
    bad = f"level,target_id,input,expected,judge_notes,grader\nskill,{a},q,a,,vibes\n"
    with pytest.raises(EvalParseError, match="grader"):
        parse_eval_file("cases.csv", bad.encode())
    with pytest.raises(EvalParseError, match="column"):
        parse_eval_file("cases.csv", b"foo,bar\n1,2\n")


def test_parse_xlsx() -> None:
    from openpyxl import Workbook

    tid = str(uuid4())
    wb = Workbook()
    ws = wb.active
    ws.append(["level", "target_id", "input", "expected", "judge_notes", "grader"])
    ws.append(["sub_agent", tid, "summarize the workspace", "a summary", "", "contains"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_eval_file("cases.xlsx", buf.getvalue())
    assert rows["level"] == "sub_agent"
    assert rows["cases"][0]["grader"] == "contains"


# ── graders (spec §15) ───────────────────────────────────────────────


async def test_exact_and_contains_graders(client: Any) -> None:
    ok = await grade_case(grader="exact", answer="  4 ", expected="4", judge_notes="")
    assert ok == {"status": "graded", "passed": True, "score": 1.0, "reason": "exact match"}
    miss = await grade_case(grader="exact", answer="five", expected="4", judge_notes="")
    assert miss["passed"] is False and miss["score"] == 0.0
    sub = await grade_case(
        grader="contains", answer="The answer is RED, clearly.", expected="red", judge_notes=""
    )
    assert sub["passed"] is True
    nosub = await grade_case(grader="contains", answer="blue", expected="red", judge_notes="")
    assert nosub["passed"] is False


async def test_llm_judge_grader_and_failure_grades_error(client: Any) -> None:
    from app.llm import fake as fake_llm

    await _set(default_model="fake:scripted")
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "j1",
                "name": "EvalVerdict",
                "args": {"passed": True, "score": 0.9, "reason": "red is a primary color"},
            }
        ],
    )
    out = await grade_case(
        grader="llm_judge",
        answer="red",
        expected="a primary color",
        judge_notes="any of red/blue/yellow passes",
    )
    assert out["passed"] is True and out["score"] == 0.9 and "primary" in out["reason"]
    # judge failure ⇒ error, never a silent pass (no scripted response queued)
    err = await grade_case(grader="llm_judge", answer="x", expected="y", judge_notes="")
    assert err["status"] == "error" and err["passed"] is False


# ── the admin-direct runner (spec §15) ───────────────────────────────


async def test_skill_eval_runs_and_grades(seeded_client: Any) -> None:
    from app.evals.runner import execute_eval_run
    from app.llm import fake as fake_llm

    await _set(default_model="fake:scripted", formatter_enabled=False)
    skill = await _seeded_skill(seeded_client)
    tid = skill["id"]
    upload = await seeded_client.post(
        "/api/v1/evals/datasets",
        files={"file": ("cases.csv", CSV.format(tid=tid).encode(), "text/csv")},
        data={"name": "m32-smoke"},
    )
    assert upload.status_code == 201, upload.text
    dataset_id = upload.json()["id"]
    assert upload.json()["case_count"] == 2

    # scripted worker answers for the two cases, then the judge verdict
    fake_llm.push_ai("4")
    fake_llm.push_ai("red")
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "id": "j2",
                "name": "EvalVerdict",
                "args": {"passed": True, "score": 1.0, "reason": "primary color named"},
            }
        ],
    )
    eval_run = await execute_eval_run(UUID(dataset_id))
    assert eval_run.status == "completed"
    assert eval_run.total_cases == 2 and eval_run.passed_cases == 2
    async with get_session_factory()() as session:
        from sqlalchemy import select

        results = list(
            (
                await session.execute(
                    select(EvalResult).where(EvalResult.eval_run_id == eval_run.id)
                )
            ).scalars()
        )
        assert len(results) == 2 and all(r.passed for r in results)
        # every result's run is an ordinary Run tagged eval=true
        for r in results:
            run = await session.get(Run, r.run_id)
            assert run is not None and run.is_eval is True
            assert run.status == "completed"


async def test_hidden_skill_is_evaluable(seeded_client: Any) -> None:
    """§15: the rung-4 'exposed skills only' rule does NOT gate eval runs."""
    from app.evals.runner import execute_eval_run
    from app.llm import fake as fake_llm

    await _set(default_model="fake:scripted", formatter_enabled=False)
    skill = await _seeded_skill(seeded_client)
    # hide it from the planner
    await seeded_client.patch(f"/api/v1/skills/{skill['id']}", json={"direct_exposure": False})
    csv = (
        "level,target_id,input,expected,judge_notes,grader\n"
        f'skill,{skill["id"]},"what is 3+3?","6",,exact\n'
    )
    upload = await seeded_client.post(
        "/api/v1/evals/datasets",
        files={"file": ("hidden.csv", csv.encode(), "text/csv")},
    )
    assert upload.status_code == 201
    fake_llm.push_ai("6")
    eval_run = await execute_eval_run(UUID(upload.json()["id"]))
    assert eval_run.status == "completed" and eval_run.passed_cases == 1


async def test_eval_run_api_surfaces_results(seeded_client: Any) -> None:
    from app.llm import fake as fake_llm

    await _set(default_model="fake:scripted", formatter_enabled=False)
    skill = await _seeded_skill(seeded_client)
    csv = (
        "level,target_id,input,expected,judge_notes,grader\n"
        f'skill,{skill["id"]},"what is 5+5?","10",,exact\n'
    )
    upload = await seeded_client.post(
        "/api/v1/evals/datasets",
        files={"file": ("api.csv", csv.encode(), "text/csv")},
    )
    dataset_id = upload.json()["id"]
    fake_llm.push_ai("10")
    started = await seeded_client.post(f"/api/v1/evals/datasets/{dataset_id}/run")
    assert started.status_code == 201
    eval_run_id = started.json()["id"]
    # the runner is synchronous-by-await in tests (background task completes)
    import asyncio

    for _ in range(100):
        detail = (await seeded_client.get(f"/api/v1/evals/runs/{eval_run_id}")).json()
        if detail["status"] != "running":
            break
        await asyncio.sleep(0.1)
    assert detail["status"] == "completed"
    assert detail["passed_cases"] == 1 and detail["total_cases"] == 1
    assert detail["results"][0]["passed"] is True
    assert detail["results"][0]["answer"].strip() == "10"
    listing = (await seeded_client.get("/api/v1/evals/datasets")).json()
    assert any(d["id"] == dataset_id for d in listing["items"])
