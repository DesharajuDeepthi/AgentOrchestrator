"""Observability (spec §10): structlog JSON, Prometheus metrics, OTel spans,
per-run LangSmith tracer. One shared label set on every span/log/metric:
{run_id, step_id, tier, kind, source, entity_id, entity_name, model, effort,
input_tokens, output_tokens, duration_ms, status}.
"""

import logging
from collections.abc import Sequence
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from prometheus_client import Counter, Gauge, Histogram

from app.config import get_config

# ── structured logs ──────────────────────────────────────────────


def configure_logging(level: str = "INFO") -> None:
    """Idempotent and re-invocable: the `log_level` setting applies live
    (spec §5b), so this runs at bootstrap AND on every settings write that
    touches log_level."""
    logging.basicConfig(level=level, format="%(message)s")
    # basicConfig is a no-op once handlers exist — set the level explicitly
    # so runtime re-configuration actually takes effect
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )


# ── metrics (spec §10): counters + histograms labeled tier/kind/source ─

RUNS_TOTAL = Counter("concierge_runs_total", "Runs started", ["mode", "status"])
STEPS_TOTAL = Counter(
    "concierge_steps_total", "Run steps recorded", ["tier", "kind", "source", "status"]
)
TOOL_CALLS_TOTAL = Counter(
    "concierge_tool_calls_total", "Tool calls executed", ["kind", "source", "status"]
)
ERRORS_TOTAL = Counter("concierge_errors_total", "Errors", ["tier", "kind", "source"])
RUN_DURATION = Histogram("concierge_run_duration_seconds", "Run duration", ["mode", "status"])
# memory layers (spec §16.6)
MEMORY_OPS = Counter("concierge_memory_ops_total", "Memory operations", ["kind", "status"])
MEMORY_INJECTED_TOKENS = Histogram(
    "concierge_memory_injected_tokens", "Tokens injected from memory", ["surface"]
)
MEMORY_RECALL_SECONDS = Histogram("concierge_memory_recall_seconds", "Memory recall latency")
# ambient mode (spec §17.6)
AMBIENT_OPS = Counter("concierge_ambient_ops_total", "Ambient operations", ["kind", "status"])
# M44: humans saving past the §4 overlap warning — capture-only telemetry
OVERLAP_OVERRIDES = Counter(
    "concierge_overlap_overrides_total", "Overlap-guard overrides", ["draft_type"]
)
A2A_OPS = Counter("concierge_a2a_ops_total", "A2A operations (spec §19)", ["kind", "status"])
# §18.1: real token cost of tier-2 significance judgments
AMBIENT_JUDGE_TOKENS = Counter(
    "concierge_ambient_judge_tokens_total", "Ambient judge token usage", ["direction"]
)
# §18.9: 1 while this replica holds the ambient-tick leader lease
AMBIENT_LEADER = Gauge("concierge_ambient_leader", "1 when this replica leads the ambient tick")
STEP_DURATION = Histogram(
    "concierge_step_duration_seconds", "Step duration", ["tier", "kind", "source"]
)
STEP_TOKENS = Histogram(
    "concierge_step_tokens", "Tokens per step", ["tier", "kind", "source", "direction"]
)

# ── OpenTelemetry ────────────────────────────────────────────────
#
# The span pipeline is always installed; where spans go is decided by a
# swappable exporter so the `otlp_endpoint` setting can override the
# OTEL_EXPORTER_OTLP_ENDPOINT env bootstrap at runtime (spec §10) —
# no restart, no processor re-registration.

_otel_configured = False


class _SwappableSpanExporter(SpanExporter):
    """Delegates to the currently configured OTLP exporter; None drops spans."""

    def __init__(self) -> None:
        self.target: SpanExporter | None = None
        self.endpoint: str | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        target = self.target
        if target is None:
            return SpanExportResult.SUCCESS
        return target.export(spans)

    def shutdown(self) -> None:
        if self.target is not None:
            self.target.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self.target is None:
            return True
        return self.target.force_flush(timeout_millis)


_exporter = _SwappableSpanExporter()


def _ensure_provider() -> None:
    global _otel_configured
    if not _otel_configured:
        provider = TracerProvider(resource=Resource.create({"service.name": "concierge-agent"}))
        provider.add_span_processor(BatchSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
        _otel_configured = True


def apply_otlp_endpoint(endpoint: str | None) -> None:
    """Point span export at a new OTLP collector live; empty/None disables.
    Called at bootstrap (env default), at startup (stored setting override),
    and on every settings write that touches otlp_endpoint."""
    _ensure_provider()
    normalized = (endpoint or "").strip() or None
    if normalized == _exporter.endpoint:
        return
    old = _exporter.target
    if normalized is None:
        _exporter.target = None
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        # the env-var path appends the signal suffix itself; the kwarg path
        # takes the URL verbatim, so normalize to the traces endpoint
        url = (
            normalized
            if normalized.endswith("/v1/traces")
            else normalized.rstrip("/") + "/v1/traces"
        )
        _exporter.target = OTLPSpanExporter(endpoint=url)
    _exporter.endpoint = normalized
    if old is not None:
        old.shutdown()


def otlp_endpoint_in_use() -> str | None:
    """The endpoint spans are currently exported to (None = export disabled)."""
    return _exporter.endpoint


def bootstrap_otel_from_env() -> None:
    """Env bootstrap (OTEL_EXPORTER_OTLP_ENDPOINT); a stored otlp_endpoint
    setting applied later in the app lifespan overrides this."""
    _ensure_provider()
    endpoint = get_config().otel_exporter_otlp_endpoint
    if endpoint:
        apply_otlp_endpoint(endpoint)


def get_tracer() -> trace.Tracer:
    _ensure_provider()
    return trace.get_tracer("concierge-agent")


# ── LangSmith (spec §10): per-run tracer from live settings ──────


def build_langsmith_callbacks(settings: dict[str, Any], run_id: str) -> list[Any]:
    """Constructed per run from settings and injected via callbacks — not
    process env — so enabling/pointing LangSmith needs no restart. The API
    key alone stays env-only."""
    if not settings.get("langsmith_enabled"):
        return []
    api_key = get_config().langsmith_api_key
    if not api_key:
        return []
    try:
        from langchain_core.tracers.langchain import LangChainTracer
        from langsmith import Client

        client = Client(
            api_url=str(settings.get("langsmith_endpoint") or "https://api.smith.langchain.com"),
            api_key=api_key,
        )
        tracer = LangChainTracer(
            project_name=str(settings.get("langsmith_project") or "concierge-agent"),
            client=client,
        )
        # run_id metadata cross-references PG traces and OTel spans
        return [tracer]
    except Exception:  # noqa: BLE001 - observability must never break runs
        structlog.get_logger("obs").warning("langsmith_tracer_failed", run_id=run_id)
        return []


def label_set(
    *,
    run_id: str,
    step_id: str | None = None,
    tier: str,
    kind: str | None = None,
    source: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """The §10 label set, attached to every span and log line."""
    return {
        "run_id": run_id,
        "step_id": step_id,
        "tier": tier,
        "kind": kind,
        "source": source,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "model": model,
        "effort": effort,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "status": status,
    }
