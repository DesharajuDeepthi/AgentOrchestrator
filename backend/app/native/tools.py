"""Static native tools (spec §5b).

`summarize-and-structure` is a single-node LangGraph subgraph exposed as a
tool: one LLM call that converts raw text into a structured JSON summary.
It proves the subgraph-as-tool path alongside MCP tools in the same skill.
"""

import json
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import AliasChoices, BaseModel, Field, model_validator

from app.db import get_session_factory
from app.llm import ModelParams, get_model
from app.native.provider import native_tool
from app.prompts import load_prompt


class StructuredSummary(BaseModel):
    """Structured output schema for summarize-and-structure."""

    title: str = Field(description="short descriptive title")
    summary: str = Field(description="faithful 2-4 sentence summary")
    key_points: list[str] = Field(description="3-7 most important points")
    entities: list[str] = Field(description="named people/organizations/products")


class _SummarizeState(TypedDict):
    text: str
    result: dict[str, Any]


async def _resolve_default_model() -> tuple[str, ModelParams | None]:
    from app.settings_store import get_setting

    async with get_session_factory()() as session:
        ref = await get_setting(session, "default_model")
        raw = await get_setting(session, "default_model_params")
    params = ModelParams.model_validate(raw) if raw else None
    return str(ref), params


def build_summarize_graph() -> Any:
    async def summarize_node(state: _SummarizeState, config: RunnableConfig) -> dict[str, Any]:
        ref, params = await _resolve_default_model()
        model = get_model(ref, params).with_structured_output(StructuredSummary)
        prompt = load_prompt("summarize_and_structure").format(text=state["text"])
        try:
            result = await model.ainvoke(prompt, config=config)
        except Exception as exc:
            # strict schema + one repair retry (same pattern as planner
            # validation, spec §7.1): feed the validation errors back once
            repair = (
                f"{prompt}\n\nYour previous attempt failed schema validation:\n{exc}\n"
                "Return a JSON object matching the schema EXACTLY — key_points and "
                "entities MUST be JSON arrays of strings."
            )
            result = await model.ainvoke(repair, config=config)
        assert isinstance(result, StructuredSummary)
        return {"result": result.model_dump()}

    graph = StateGraph(_SummarizeState)
    graph.add_node("summarize", summarize_node)
    graph.add_edge(START, "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()


@native_tool(
    "summarize-and-structure",
    "Convert raw text into a structured JSON summary (title, summary, key points, entities).",
)
async def summarize_and_structure(
    text: str, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Invoke the single-node subgraph; callbacks in `config` capture usage."""
    graph = build_summarize_graph()
    out: dict[str, Any] = await graph.ainvoke({"text": text}, config=config)
    return dict(out["result"])


class _ChartSeries(BaseModel):
    # Chart.js-style `label`/`data` accepted as validation aliases so a
    # first call in that common convention succeeds instead of burning a
    # repair round-trip; canonical dump stays `name`/`values` (the renderer
    # contract in ChartSvg.tsx)
    name: str = Field("", validation_alias=AliasChoices("name", "label"))
    values: list[float] = Field(
        default_factory=list, validation_alias=AliasChoices("values", "data")
    )
    # scatter/bubble points: [x, y] or [x, y, size] per point
    points: list[list[float]] | None = None
    # combo charts: how this series draws
    render: Literal["bar", "line"] | None = None


ChartKind = Literal[
    "bar",
    "hbar",
    "stacked_bar",
    "stacked_bar_100",
    "line",
    "area",
    "stacked_area",
    "pie",
    "donut",
    "histogram",
    "funnel",
    "waterfall",
    "lollipop",
    "gauge",
    "sparkline",
    "scatter",
    "bubble",
    "candlestick",
    "boxplot",
    "gantt",
    "combo",
]

# kinds where per-series values align 1:1 with labels (the default contract)
_ALIGNED_KINDS = {
    "bar",
    "hbar",
    "stacked_bar",
    "stacked_bar_100",
    "line",
    "area",
    "stacked_area",
    "pie",
    "donut",
    "histogram",
    "funnel",
    "waterfall",
    "lollipop",
    "combo",
    "candlestick",
    "boxplot",
}
_NAMED_SERIES_KINDS = {
    "candlestick": ["open", "high", "low", "close"],
    "boxplot": ["min", "q1", "median", "q3", "max"],
}


class _ChartSpec(BaseModel):
    """render_chart args (spec §5b): data the caller actually holds."""

    kind: ChartKind
    title: str = ""
    labels: list[str]
    series: list[_ChartSeries]
    # gantt only: [startISO, endISO] per label, e.g. ["2026-08-01", "2026-08-05"]
    ranges: list[list[str]] | None = None

    @model_validator(mode="after")
    def _kind_shape(self) -> "_ChartSpec":
        if self.kind in {"scatter", "bubble"}:
            need = 3 if self.kind == "bubble" else 2
            if not self.series or not all(s.points for s in self.series):
                raise ValueError(
                    f"{self.kind} needs points ([x, y{', size' if need == 3 else ''}]) on every series"
                )
            for s in self.series:
                for p in s.points or []:
                    if len(p) < need:
                        raise ValueError(f"{self.kind} points need {need} numbers each")
            return self
        if self.kind == "gauge":
            if len(self.series) != 1 or len(self.series[0].values) != 2:
                raise ValueError("gauge needs exactly one series with [value, max]")
            return self
        if self.kind == "sparkline":
            if not self.series or not self.series[0].values:
                raise ValueError("sparkline needs one series of values")
            return self
        if self.kind == "gantt":
            if not self.ranges or len(self.ranges) != len(self.labels):
                raise ValueError("gantt needs ranges ([startISO, endISO]) aligned 1:1 with labels")
            for r in self.ranges:
                if len(r) != 2:
                    raise ValueError("each gantt range is [start, end]")
            return self
        expected = _NAMED_SERIES_KINDS.get(self.kind)
        if expected:
            names = sorted(s.name.lower() for s in self.series)
            if names != sorted(expected):
                raise ValueError(f"{self.kind} needs exactly these series: {', '.join(expected)}")
        if (
            self.kind in {"funnel", "waterfall", "pie", "donut", "histogram"}
            and len(self.series) > 1
        ):
            # single-series kinds: extra series are almost always a mistake
            raise ValueError(f"{self.kind} takes exactly one series")
        if self.kind in _ALIGNED_KINDS:
            for entry in self.series:
                if len(entry.values) != len(self.labels):
                    raise ValueError(
                        f"series {entry.name or '?'!r} has {len(entry.values)} values "
                        f"for {len(self.labels)} labels"
                    )
        return self


@native_tool(
    "render_chart",
    "Validate and normalize a chart specification from data you already hold — "
    "rendered as a real chart in the final answer panel. Kinds: bar / hbar "
    "(horizontal) / stacked_bar / stacked_bar_100 (normalized %), line / area / "
    "stacked_area (trends), pie / donut (shares, one series), histogram "
    "(pre-binned ONLY: bin-range labels + counts — never bin raw values "
    "yourself), funnel (ordered stages, one series), waterfall (signed deltas, "
    "one series), lollipop, gauge (one series [value, max]), sparkline (tiny "
    "trend), scatter / bubble (per-series points [x,y] / [x,y,size]), "
    "candlestick (four series named open/high/low/close), boxplot (five series "
    "named min/q1/median/q3/max — pre-computed only), gantt (ranges: "
    '[startISO, endISO] per label), combo (per-series render: "bar"|"line"). '
    'Default shape: {"name": str, "values": [numbers]} aligned 1:1 with labels. '
    "Use ONLY real data from the conversation or tool results, never invented "
    "numbers.",
)
async def render_chart(
    kind: str,
    labels: list[str],
    series: list[_ChartSeries],
    title: str = "",
    ranges: list[list[str]] | None = None,
) -> str:
    """Pure validation/normalization — no model call, no side effects."""
    spec = _ChartSpec.model_validate(
        {"kind": kind, "title": title, "labels": labels, "series": series, "ranges": ranges}
    )
    # the tool result is the model's only observation of what happened — say
    # explicitly that a real chart WILL render, or models hedge with ASCII
    # duplicates of the same data in their prose answer
    return json.dumps(
        {
            "status": (
                "chart accepted — it will be rendered as a real chart in the "
                "answer panel; do not draw an ASCII/text version of this data, "
                "and refer to the chart WITHOUT positional words like 'above' "
                "or 'below' (its position varies by view)"
            ),
            "spec": spec.model_dump(exclude_none=True),
        }
    )
