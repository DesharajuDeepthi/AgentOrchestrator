# Observability

Four layers, one shared label set (spec §10). The always-on layer is the Postgres run trace; structured logs, Prometheus metrics, OpenTelemetry spans, and LangSmith traces are emitted alongside it, all correlated by `run_id`. The implementation lives in `backend/app/obs.py` (logging, metrics, OTel, LangSmith, label set) and `backend/app/orchestrator/recorder.py` (per-step recording and emission).

## Always-on: Postgres run traces

Every chat message becomes a `runs` row; every unit of work inside it becomes a `run_steps` row (spec §3.6, models in `backend/app/models/`). No toggle — this is the source of truth.

**Per run**: `conversation_id`, `chat_message`, `plan` (the planner's validated JSON, or the agentic todo list), `snapshot` (the resolved persona/workflow/model/tool definitions frozen at dispatch, so later registry edits never rewrite history), `answer_ui` (the persisted structured-summary payload), `status` (`running | paused_hitl | completed | failed | cancelled`), timestamps, and rolled-up `total_input_tokens` / `total_output_tokens`.

**Per step** (`RunRecorder.start_step` / `finish_step`): `step_type` (`plan | route | skill | hitl | tool_call | aggregate`), `parent_step_id` (nesting — e.g. tool calls under their skill node, native-subgraph children under their `tool_call`), `sub_agent_id`, `node_id`, `input`/`output` jsonb, `model` (the `provider:model` reference actually used), `input_tokens`/`output_tokens`, `started_at`/`finished_at`, `error`, `status`. Ephemeral dynamic workers appear with their per-run callsign plus composition as `entity_name` — e.g. `worker-alpha (web-research+file-ops)` (`backend/app/orchestrator/ladder.py`, callsign sequence in `backend/app/orchestrator/context.py`).

`route` steps record the resolution-ladder decision: `output.rung` is one of `direct_tool | direct_skill | native_sub_agent | custom_sub_agent | dynamic_worker`, or `fallback` when the full-catalog fallback engaged (`RunRecorder.record_route`).

**How the Runs page renders it** (`frontend/src/pages/RunsPage.tsx`): the table shows time, message excerpt, status pill, orchestrator-mode badge, duration, and `input→output` token totals. The detail drawer rebuilds the step tree from `parent_step_id`, showing per step: an icon per `step_type`, `node_id`, a `rung:` chip on route steps (highlighted for `fallback`), the model reference, token counts, duration, status, and expandable input/output JSON plus any error text. Below the timeline sit the raw plan JSON and the frozen config snapshot. Paused runs expose Approve/Deny inline; failed runs expose Retry; running runs expose Cancel.

## The shared label set (spec §10)

`obs.label_set()` defines the real, exhaustive set — attached to every span and log line:

```
run_id, step_id, tier, kind, source, entity_id, entity_name,
model, effort, input_tokens, output_tokens, duration_ms, status
```

- `tier`: `tool | skill | sub_agent | orchestrator`.
- `kind`: per-tier discriminator — tools `mcp | native`; skills and sub agents `native | custom`; sub-agent tier additionally `dynamic` for ephemeral workers.
- `source`: `static | dynamic` (seeded vs UI-created registry record).
- `model` / `effort`: the resolved `provider:model` reference and normalized effort level for the step.

Where it is attached:

- **Logs**: `step_start` / `step_finish` events in `recorder.py` spread the full label set into the structlog event.
- **Spans**: every step opens an OTel span named `{step_type}:{entity_name|node_id|tier}` with each label as a `concierge.*` attribute, plus `concierge.status` and `concierge.duration_ms` at close. Span nesting mirrors `run_steps.parent_step_id`.
- **Metrics**: the low-cardinality subset (`tier`, `kind`, `source`, plus `mode`, `status`, `direction`) labels the Prometheus series below — `run_id`-class labels are deliberately kept out of metrics.

## Structured logging

`structlog` (verified: `backend/app/obs.py::configure_logging`) rendering JSON to stdout — shippable to ELK/Filebeat with no parsing config. Processor chain: contextvars merge, log level, ISO timestamps, `format_exc_info` (tracebacks render as structured fields), `JSONRenderer`.

What gets logged: one event per run step start/finish (with the full label set), registry mutations, MCP lifecycle events (connect, ingest, `listChanged` reconcile, health failures), retrieval truncations (`retrieval_truncated_catalog` with total/shown/dropped counts), and errors. **No-secrets rule**: provider API keys never pass through application code paths that log (they exist only inside `AppConfig` and the provider adapters); MCP env/header values are logged nowhere — the rule is stated in `backend/app/config.py` and holds throughout `app/`.

The bootstrap log level comes from the `LOG_LEVEL` env var (`main.py::create_app`). The `log_level` setting (Settings → Observability) overrides it live: a PATCH re-runs `configure_logging` immediately — stdlib root level and the structlog filter both move, no restart — and an explicitly stored value is re-applied over the env bootstrap at every startup (`main.py` lifespan). If the setting has never been touched, the env value stands.

## Optional: OpenTelemetry

`OTEL_EXPORTER_OTLP_ENDPOINT` in the environment (`.env` / compose) is the bootstrap default; the `otlp_endpoint` setting (Settings → Observability) overrides it at runtime (spec §10). The span pipeline is always installed at app creation: a `TracerProvider` with `service.name=concierge-agent` and a `BatchSpanProcessor` around a swappable exporter (`obs._SwappableSpanExporter`). A settings PATCH repoints the exporter live — new OTLP/HTTP exporter in, old one shut down; an empty value disables export (spans are created but dropped — zero-config no-op). An explicitly stored setting is re-applied over the env bootstrap at startup. Point it at Tempo, Jaeger, or any OTLP collector. Instrumented: every recorded run step (orchestrator plan/route/aggregate, skill nodes, tool calls, HITL), with nesting mirroring the step tree.

## Optional: LangSmith (hot-switchable)

Framework-level traces (prompts, completions, tool calls) for every LLM touchpoint. The switches are **runtime settings**, not env (`app_settings`: `langsmith_enabled`, `langsmith_endpoint`, `langsmith_project` — editable live in Settings → Observability): `obs.build_langsmith_callbacks(settings, run_id)` constructs a `LangChainTracer` **per run** from the current settings and injects it via callbacks (`backend/app/orchestrator/runner.py`), so enabling, disabling, or repointing LangSmith requires no restart. An empty endpoint means SaaS (`https://api.smith.langchain.com`); set it to a self-hosted instance URL to keep traces local. Only `LANGSMITH_API_KEY` stays env-only — no key, no tracer, silently. Tracer construction failures log a warning and never break the run. LangSmith runs simultaneously with OTel; `run_id` cross-references the Postgres trace.

## Metrics: `/metrics` (Prometheus)

Served by the backend at `GET /metrics` (`main.py`) and also proxied by the frontend nginx (`frontend/nginx.conf` has an explicit `/metrics` location). Series defined in `obs.py`:

| Metric | Type | Labels |
|---|---|---|
| `concierge_runs_total` | counter | `mode`, `status` |
| `concierge_steps_total` | counter | `tier`, `kind`, `source`, `status` |
| `concierge_tool_calls_total` | counter | `kind`, `source`, `status` |
| `concierge_errors_total` | counter | `tier`, `kind`, `source` |
| `concierge_run_duration_seconds` | histogram | `mode`, `status` |
| `concierge_step_duration_seconds` | histogram | `tier`, `kind`, `source` |
| `concierge_step_tokens` | histogram | `tier`, `kind`, `source`, `direction` |

Grafana can slice native vs custom vs mcp directly off the `kind`/`source` labels.

## Token usage tracking

All token accounting flows through LangChain's `usage_metadata` on messages returned by the provider port — never a provider SDK (spec §2.1 neutrality rule). Collection points: the planner (`planner.py`), the aggregator token stream (`graph_mode.py`), skill/fallback loops (`ladder.py`), the answer-UI generation call (`answer_ui.py`), and worker skill nodes (`factory/worker.py`); native subgraph tools report nested LLM usage via a callback handler so their tokens land on the `tool_call` step and roll up correctly (spec §5b guardrail 3).

Surfaced: per step in `run_steps.input_tokens/output_tokens`, rolled up to `runs.total_input_tokens/total_output_tokens` by `RunRecorder.finish_step`, shown in the Runs table (`in→out` column), per-step in the trace timeline, in the `concierge_step_tokens` histogram, and in every step's log line and span attributes. The chat `done` SSE event also carries run token totals.
