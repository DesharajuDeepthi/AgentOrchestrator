"""The formatter role (spec §7.1): artifact carries presentation + coverage,
off = no artifact, tool charts are formatter-independent."""

import json
from typing import Any

from httpx import AsyncClient

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.models import Run, RunStep
from app.orchestrator.answer_ui import AnswerUi, UiComponent, compute_coverage
from app.orchestrator.runner import _collect_tool_charts
from app.settings_store import update_settings


def _ui(*components: UiComponent) -> AnswerUi:
    return AnswerUi(components=list(components))


class TestCoverage:
    def test_full_retention_scores_100(self) -> None:
        answer = "Revenue grew 12.4% to $3.2M — see https://x.io/report and `run_id`."
        ui = _ui(UiComponent(type="text", markdown=answer))
        assert compute_coverage(answer, ui) == 100

    def test_dropped_tokens_lower_the_score(self) -> None:
        answer = "Line 3 made 120 units, line 4 made 80 units, uptime 99.5%."
        ui = _ui(UiComponent(type="stat", label="line 3", value="120"))
        score = compute_coverage(answer, ui)
        assert score < 100
        assert score >= 20  # at least the retained number counts

    def test_prose_only_answer_scores_100(self) -> None:
        answer = "The approach is sound and the team agrees on the direction."
        ui = _ui(UiComponent(type="text", markdown="A different paraphrase."))
        assert compute_coverage(answer, ui) == 100  # no hard tokens to lose


class TestFormatterPayload:
    async def test_payload_carries_presentation_and_coverage(self) -> None:
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(components=[UiComponent(type="text", markdown="All 3 facts kept.")])
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "u1"}]
            )
        )
        payload, _usage = await generate_answer_ui(
            "fake:scripted", "t", "There are 3 facts.", [], presentation="a2ui_first"
        )
        assert payload is not None
        assert payload["presentation"] == "a2ui_first"
        assert payload["coverage"] == 100

    async def test_blocks_preserve_chart_position(self) -> None:
        """A chart placed between two text sections must survive as an
        ordered blocks list — segment, chart, segment (spec §8.5)."""
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(
            components=[
                UiComponent(type="text", markdown="The trend was up: 120 then 135."),
                UiComponent(
                    type="chart",
                    chart_kind="bar",
                    title="T",
                    labels=["a", "b"],
                    series=[{"name": "s", "values": [120.0, 135.0]}],  # type: ignore[list-item]
                ),
                UiComponent(type="text", markdown="Which is why we recommend rollout."),
            ]
        )
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "u2"}]
            )
        )
        payload, _usage = await generate_answer_ui(
            "fake:scripted", "t", "120 then 135.", [], presentation="a2ui_first"
        )
        assert payload is not None
        kinds = [
            next(k for k in ("a2ui", "chart", "tool_chart_ref") if k in b)
            for b in payload["blocks"]
        ]
        assert kinds == ["a2ui", "chart", "a2ui"]
        assert payload["blocks"][1]["chart"]["kind"] == "bar"
        assert payload["charts"], "parallel charts array stays for legacy/history consumers"

    async def test_tool_chart_ref_placement_and_guards(self) -> None:
        """Refs place tool charts mid-flow; invalid and duplicate refs are
        dropped so the renderer's bottom slot stays the safety net."""
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(
            components=[
                UiComponent(type="text", markdown="Weekly changes below."),
                UiComponent(type="chart", ref=0),
                UiComponent(type="text", markdown="And the same chart again:"),
                UiComponent(type="chart", ref=0),  # duplicate — dropped
                UiComponent(type="chart", ref=7),  # out of range — dropped
                UiComponent(type="text", markdown="Done."),
            ]
        )
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "u3"}]
            )
        )
        tool_charts = [{"kind": "line", "title": "W", "labels": ["a"], "series": []}]
        payload, _usage = await generate_answer_ui(
            "fake:scripted", "t", "answer", [], presentation="a2ui_first", tool_charts=tool_charts
        )
        assert payload is not None
        kinds = [
            next(k for k in ("a2ui", "chart", "tool_chart_ref") if k in b)
            for b in payload["blocks"]
        ]
        assert kinds == ["a2ui", "tool_chart_ref", "a2ui"]
        assert payload["blocks"][1]["tool_chart_ref"] == 0
        assert "charts" not in payload  # refs alone add no formatter charts

    async def test_table_becomes_native_block_at_its_position(self) -> None:
        """Tables leave the catalog text-row path: they render as native
        themed table blocks exactly where the formatter placed them."""
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(
            components=[
                UiComponent(type="text", markdown="Comparison below."),
                UiComponent(
                    type="table",
                    columns=["Line", "Units"],
                    rows=[["3", "120"], ["4", "80"]],
                ),
                UiComponent(type="text", markdown="Line 4 trails."),
            ]
        )
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "u5"}]
            )
        )
        payload, _usage = await generate_answer_ui("fake:scripted", "t", "answer", [])
        assert payload is not None
        kinds = [
            next(k for k in ("a2ui", "chart", "tool_chart_ref", "table") if k in b)
            for b in payload["blocks"]
        ]
        assert kinds == ["a2ui", "table", "a2ui"]
        assert payload["blocks"][1]["table"] == {
            "columns": ["Line", "Units"],
            "rows": [["3", "120"], ["4", "80"]],
        }

    async def test_donut_chart_kind_flows_through(self) -> None:
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(
            components=[
                UiComponent(type="text", markdown="Shares: 45, 30, 25."),
                UiComponent(
                    type="chart",
                    chart_kind="donut",
                    labels=["A", "B", "C"],
                    series=[{"name": "", "values": [45.0, 30.0, 25.0]}],  # type: ignore[list-item]
                ),
            ]
        )
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "u6"}]
            )
        )
        payload, _usage = await generate_answer_ui("fake:scripted", "t", "45 30 25", [])
        assert payload is not None
        assert payload["charts"][0]["kind"] == "donut"
        assert payload["blocks"][1]["chart"]["kind"] == "donut"

    async def test_unplaced_tool_charts_trigger_repair(self) -> None:
        """Ref placement is enforced in code, not just prompted: a document
        that leaves tool charts unplaced gets ONE repair invocation."""
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        no_refs = AnswerUi(components=[UiComponent(type="text", markdown="Trend discussed.")])
        with_ref = AnswerUi(
            components=[
                UiComponent(type="text", markdown="Trend discussed."),
                UiComponent(type="chart", ref=0),
                UiComponent(type="text", markdown="After."),
            ]
        )
        fake_llm.push_message(
            AIMessage(
                content="",
                tool_calls=[{"name": "AnswerUi", "args": no_refs.model_dump(), "id": "r1"}],
            )
        )
        fake_llm.push_message(
            AIMessage(
                content="",
                tool_calls=[{"name": "AnswerUi", "args": with_ref.model_dump(), "id": "r2"}],
            )
        )
        tool_charts = [{"kind": "line", "title": "W", "labels": ["a"], "series": []}]
        payload, _usage = await generate_answer_ui(
            "fake:scripted", "t", "answer", [], tool_charts=tool_charts
        )
        assert payload is not None
        kinds = [
            next(k for k in ("a2ui", "chart", "tool_chart_ref", "table") if k in b)
            for b in payload["blocks"]
        ]
        assert kinds == ["a2ui", "tool_chart_ref", "a2ui"], "repair result must be used"

    async def test_chart_ask_without_charts_triggers_repair(self) -> None:
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        text_only = AnswerUi(components=[UiComponent(type="text", markdown="Values: 1, 2, 3.")])
        with_chart = AnswerUi(
            components=[
                UiComponent(type="text", markdown="Values: 1, 2, 3."),
                UiComponent(
                    type="chart",
                    chart_kind="bar",
                    labels=["a", "b", "c"],
                    series=[{"name": "", "values": [1.0, 2.0, 3.0]}],  # type: ignore[list-item]
                ),
            ]
        )
        fake_llm.push_message(
            AIMessage(
                content="",
                tool_calls=[{"name": "AnswerUi", "args": text_only.model_dump(), "id": "c1"}],
            )
        )
        fake_llm.push_message(
            AIMessage(
                content="",
                tool_calls=[{"name": "AnswerUi", "args": with_chart.model_dump(), "id": "c2"}],
            )
        )
        payload, _usage = await generate_answer_ui(
            "fake:scripted", "please chart the values", "Values: 1, 2, 3.", []
        )
        assert payload is not None
        assert payload["charts"], "repair must yield a chart when the user asked for one"

    def test_place_missing_refs_matches_narrative(self) -> None:
        from app.orchestrator.answer_ui import place_missing_refs

        ui = AnswerUi(
            components=[
                UiComponent(type="text", markdown="Revenue grew each month this quarter."),
                UiComponent(type="text", markdown="The trial funnel shows a big activation leak."),
            ]
        )
        charts = [{"kind": "funnel", "title": "Trial Funnel", "labels": ["Trials", "Activated"]}]
        forced = place_missing_refs(ui, charts)
        assert forced == [0]
        # inserted right after the funnel narrative, not at the end by accident
        assert ui.components[2].type == "chart" and ui.components[2].ref == 0

    async def test_stubborn_model_still_gets_refs_force_placed(self) -> None:
        """Even if BOTH attempts ignore the ref contract, the code places the
        tool charts deterministically — placement never depends on the model."""
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        chartless = AnswerUi(
            components=[UiComponent(type="text", markdown="The weekly trend is discussed here.")]
        )
        for i in range(2):
            fake_llm.push_message(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "AnswerUi", "args": chartless.model_dump(), "id": f"s{i}"}
                    ],
                )
            )
        tool_charts = [{"kind": "line", "title": "Weekly Trend", "labels": ["w1"], "series": []}]
        payload, _usage = await generate_answer_ui(
            "fake:scripted", "t", "answer", [], tool_charts=tool_charts
        )
        assert payload is not None
        kinds = [
            next(k for k in ("a2ui", "chart", "tool_chart_ref", "table") if k in b)
            for b in payload["blocks"]
        ]
        assert "tool_chart_ref" in kinds, f"forced ref placement missing: {kinds}"

    async def test_compliant_document_needs_no_repair(self) -> None:
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(components=[UiComponent(type="text", markdown="No charts requested.")])
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "n1"}]
            )
        )
        payload, _usage = await generate_answer_ui("fake:scripted", "summarize this", "text", [])
        assert payload is not None  # single invocation, no repair consumed

    async def test_no_midflow_chart_means_no_blocks(self) -> None:
        from langchain_core.messages import AIMessage

        from app.orchestrator.answer_ui import generate_answer_ui

        ui = AnswerUi(components=[UiComponent(type="text", markdown="Just prose.")])
        fake_llm.push_message(
            AIMessage(
                content="", tool_calls=[{"name": "AnswerUi", "args": ui.model_dump(), "id": "u4"}]
            )
        )
        payload, _usage = await generate_answer_ui("fake:scripted", "t", "answer", [])
        assert payload is not None
        assert "blocks" not in payload

    async def test_formatter_off_produces_no_artifact(self, client: AsyncClient) -> None:
        async with get_session_factory()() as session:
            await update_settings(
                session, {"formatter_enabled": False, "default_model": "fake:scripted"}
            )
        resp = await client.post("/api/v1/chat", json={"message": "hello"})
        assert resp.status_code == 201
        run_id = resp.json()["run_id"]
        import asyncio

        for _ in range(80):
            await asyncio.sleep(0.1)
            run = (await client.get(f"/api/v1/runs/{run_id}")).json()
            if run["status"] in {"completed", "failed"}:
                break
        assert run["status"] == "completed"
        assert run["answer_ui"] is None  # no artifact — no toggle, ever


class TestToolCharts:
    async def _make_run_with_chart_step(self, chart: dict[str, Any] | str) -> Run:
        from app.models import Conversation

        async with get_session_factory()() as session:
            conv = Conversation(title="t")
            session.add(conv)
            await session.flush()
            run = Run(conversation_id=conv.id, chat_message="m", status="completed")
            session.add(run)
            await session.flush()
            session.add(
                RunStep(
                    run_id=run.id,
                    step_type="tool_call",
                    node_id="render_chart",
                    status="completed",
                    output={"result": chart if isinstance(chart, str) else json.dumps(chart)},
                )
            )
            await session.commit()
            await session.refresh(run)
            return run

    async def test_render_chart_steps_are_collected(self) -> None:
        # current tool output: status envelope with the spec nested under "spec"
        spec = {
            "kind": "bar",
            "title": "T",
            "labels": ["a", "b"],
            "series": [{"name": "s", "values": [1.0, 2.0]}],
        }
        run = await self._make_run_with_chart_step({"status": "chart accepted", "spec": spec})
        charts = await _collect_tool_charts(run.id)
        assert charts == [spec]

    async def test_legacy_bare_spec_output_still_collected(self) -> None:
        # runs persisted before the envelope change stored the bare spec
        spec = {
            "kind": "bar",
            "title": "T",
            "labels": ["a", "b"],
            "series": [{"name": "s", "values": [1.0, 2.0]}],
        }
        run = await self._make_run_with_chart_step(spec)
        charts = await _collect_tool_charts(run.id)
        assert charts == [spec]

    async def test_invalid_chart_output_is_skipped(self) -> None:
        run = await self._make_run_with_chart_step("{truncated json")
        assert await _collect_tool_charts(run.id) == []

    async def test_non_chart_tools_are_ignored(self) -> None:
        from app.models import Conversation

        async with get_session_factory()() as session:
            conv = Conversation(title="t")
            session.add(conv)
            await session.flush()
            run = Run(conversation_id=conv.id, chat_message="m", status="completed")
            session.add(run)
            await session.flush()
            session.add(
                RunStep(
                    run_id=run.id,
                    step_type="tool_call",
                    node_id="read_file",
                    status="completed",
                    output={"result": '{"kind": "bar"}'},
                )
            )
            await session.commit()
            assert await _collect_tool_charts(run.id) == []


class TestSettingsValidation:
    async def test_presentation_validated(self, client: AsyncClient) -> None:
        resp = await client.patch("/api/v1/settings", json={"formatter_presentation": "sideways"})
        assert resp.status_code == 422
        resp = await client.patch("/api/v1/settings", json={"formatter_presentation": "raw_first"})
        assert resp.status_code == 200

    async def test_coverage_threshold_bounds(self, client: AsyncClient) -> None:
        assert (
            await client.patch("/api/v1/settings", json={"formatter_coverage_flag_threshold": 0})
        ).status_code == 422
        assert (
            await client.patch("/api/v1/settings", json={"formatter_coverage_flag_threshold": 95})
        ).status_code == 200

    async def test_formatter_model_validated_like_other_roles(self, client: AsyncClient) -> None:
        resp = await client.patch("/api/v1/settings", json={"formatter_model": "nope:not-a-model"})
        assert resp.status_code == 422
        resp = await client.patch("/api/v1/settings", json={"formatter_model": "fake:scripted"})
        assert resp.status_code == 200
