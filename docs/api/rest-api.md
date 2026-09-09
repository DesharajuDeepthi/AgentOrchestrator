# REST API Reference

The Concierge Agent backend is a single FastAPI process. All application routers are mounted under the **`/api/v1`** prefix (`backend/app/main.py`, `backend/app/api/__init__.py`). Two operational endpoints — `/health` and `/metrics` — live at the application root, *outside* `/api/v1`.

Interactive OpenAPI documentation is served by FastAPI at **`/docs`** on the running backend (e.g. `http://localhost:8000/docs` on a `docker compose up` stack).

Related documents: [SSE event stream](sse-events.md) · [Workflow DSL](workflow-dsl.md) · [Skill document format](skill-format.md)

## Conventions

- **Content type**: JSON request and response bodies (`application/json`), except `GET /api/v1/chat/stream/{run_id}` (`text/event-stream`, see [sse-events.md](sse-events.md)) and `GET /metrics` (Prometheus text format).
- **Error envelope**: every error is FastAPI's standard shape — `{"detail": <string or validation array>}`. Application errors raised via `HTTPException` carry a human-readable string in `detail`; Pydantic request-validation failures carry FastAPI's structured array. Multiple application validation errors are joined into one string with `"; "` (see `backend/app/api/skills.py`, `backend/app/api/settings.py`).
- **Request models forbid unknown fields**: all input schemas extend `ApiModel` with `extra="forbid"` (`backend/app/schemas/common.py`), so an unrecognized body field is a 422 — including any attempt to send `id` in a PATCH. **Registry `id`s are immutable**: no write schema accepts one.
- **Soft delete**: registry DELETEs set `deleted_at`; deleted records 404 on subsequent GET/PATCH (`fetch_or_404` in `backend/app/api/deps.py`).

### Status codes used by this API

| Code | Meaning here |
|------|--------------|
| 200 | Success. |
| 201 | Created — registry POSTs, `POST /chat`, `POST /runs/{id}/retry`, `POST /conversations`. |
| 204 | Success, no body — DELETEs. |
| 403 | **Static-record guard**: PATCHing any field of a `source: "static"` record other than `status`/`direct_exposure` (`enforce_static_rules`, `backend/app/api/deps.py`); DELETEing a static record (`reject_static_delete`); PATCHing `workflow`/`persona` of a `kind: "native"` sub-agent. |
| 404 | Record not found or soft-deleted. Also returned by the `/_fake/*` endpoints when `FAKE_LLM_ENABLED` is not set (the router hides itself). |
| 409 | Conflict: HITL decision posted to a run that is not `paused_hitl`; cancel/retry of a run in the wrong state; delete of a `running` run; delete of a tool/skill/MCP server that other records still bind; `tool_key` collision on tool PATCH. |
| 422 | Validation: Pydantic request-shape errors; empty chat message; unknown/inactive `tool_ids` on skill save; `{tool:...}` mention of an unbound tool; invalid `model`/`model_params` selection; workflow DAG validation and compile failures ([workflow-dsl.md](workflow-dsl.md)); settings validation — including `registry_cache_mode: "redis"` without `REDIS_URL` set in the environment (`backend/app/settings_store.py`); unknown registry name on `POST /cache/refresh/{registry}`. |
| 503 | MCP manager not running (reconnect/refresh-tools during startup/shutdown). |

### Common list query parameters

All registry list endpoints (`/tools`, `/skills`, `/sub-agents`, `/mcp-servers`) accept (`backend/app/api/deps.py`):

| Param | Type | Meaning |
|-------|------|---------|
| `include_deleted` | bool, default `false` | Include soft-deleted records. |
| `source` | `static` \| `dynamic` | Filter by record source. |
| `q` | string | Case-insensitive substring match on `name` or `description`. |

### Static-record rules (spec §4)

Records with `source: "static"` (native tools, native skills, seeded sub-agents/servers) are code- or seed-defined:

- **Definition writes are rejected with 403.** Only `status` and `direct_exposure` are togglable (`STATIC_TOGGLABLE` in `backend/app/api/deps.py`).
- **Deletes are rejected with 403** — toggle `status` to `"inactive"` instead.
- `kind: "native"` sub-agents additionally reject `workflow`/`persona` changes even where the record is not static.

---

## Settings router — `backend/app/api/settings.py`

| Method + path | Purpose |
|---|---|
| `GET /api/v1/settings` | Merged settings view: defaults overlaid with stored rows. Returns the full settings object (keys listed in `DEFAULTS`, `backend/app/settings_store.py`). |
| `PATCH /api/v1/settings` | Partial update; body is `{key: value, ...}`. 422 with joined error string on any invalid key/value. Selecting `registry_cache_mode: "redis"` requires `REDIS_URL` (422 otherwise) and pings Redis at save time (422 `redis unreachable: ...` on failure). Returns the merged settings. |
| `GET /api/v1/providers` | Read-only provider adapter panel: `[{provider_id, configured, models: [{id, display_name, supports_effort, supports_temperature, supports_max_output_tokens}]}]`. API keys are env-only and never appear here. |
| `GET /api/v1/hitl/pending` | All runs currently `paused_hitl`, across every conversation: `[{run_id, conversation_id, chat_message, started_at}]`. |

Notable settings keys: `orchestrator_mode` (`graph`/`agentic`), `default_model` / `planner_model` / `aggregator_model` (+ `*_params`), `max_parallel_dispatch`, `max_plan_steps`, `max_tool_iterations`, `orchestrator_full_fallback_enabled`, `dynamic_worker_fallback_enabled`, `formatter_enabled`, `formatter_presentation`, `formatter_model` (+ `_params`), `formatter_coverage_flag_threshold`, `answer_ui_charts_enabled`, `registry_cache_mode` (`bypass`/`memory`/`redis`), `retrieval_enabled`, `retrieval_threshold`, `retrieval_top_k`, `embedding_model`, `log_level`, `langsmith_*`, `otlp_endpoint`, `mcp_health_interval_s`, `direct_exposure_cap_warning`.

## Tools router — `backend/app/api/tools.py` (prefix `/tools`)

Tools are **never created via API** — they come from MCP ingestion or the native `@native_tool` startup scan.

| Method + path | Purpose | Key request fields | Errors |
|---|---|---|---|
| `GET /tools` | List tools (common filters). | — | — |
| `GET /tools/{tool_id}` | One tool. | — | 404 |
| `PATCH /tools/{tool_id}` | Edit `description`, `status`, `direct_exposure`, `tool_key`. | `ToolPatch` | 403 static guard (only `status`/`direct_exposure` on static); 409 `tool_key` already in use |
| `GET /tools/{tool_id}/skills` | Skills bound to this tool. | — | 404 |
| `DELETE /tools/{tool_id}` | Soft-delete a dynamic tool. | — | 403 static; 409 if bound to skills (`"tool is bound to skills: ..."`) |

`ToolOut` fields: registry base (`id`, `name`, `description`, `source`, `status`, `created_at`, `updated_at`, `deleted_at`) + `kind` (`mcp`/`native`), `mcp_server_id`, `tool_name`, `native_ref`, `tool_key`, `direct_exposure`, `input_schema`.

## Skills router — `backend/app/api/skills.py` (prefix `/skills`)

| Method + path | Purpose | Key request fields | Errors |
|---|---|---|---|
| `GET /skills` | List skills (common filters). | — | — |
| `POST /skills` | Create a custom skill (201). | `name`, `description`, `persona`, `instructions`, `tool_ids: [UUID]`, `direct_exposure`, `model`, `model_params`, `max_tool_iterations` | 422: unknown/inactive tool id; `model_params` without `model`; invalid model selection; `{tool:...}` mention not in bound tools |
| `POST /skills/check-overlap` | Advisory pre-save LLM-as-judge duplicate check — never blocks. | `name`, `description`, `instructions`, `tool_ids`, `exclude_id` | — |
| `GET /skills/{skill_id}` | One skill (embeds bound `tools`). | — | 404 |
| `PATCH /skills/{skill_id}` | Partial update; re-validates tools/model/mentions on touched fields. | `SkillPatch` (adds `status`) | 403 static guard; 422 as on create |
| `GET /skills/{skill_id}/sub-agents` | Sub-agents whose workflows use this skill. | — | 404 |
| `DELETE /skills/{skill_id}` | Soft-delete. | — | 403 static; 409 if referenced by active sub-agents |

Overlap check response (`OverlapCheckOut`, `backend/app/overlap.py`): `{overlap, threshold, overlap_percent, match_type, match_id, match_name, reasoning}`.

See [skill-format.md](skill-format.md) for the `.skill.md` document shape these fields mirror.

## Sub-agents router — `backend/app/api/sub_agents.py` (prefix `/sub-agents`)

| Method + path | Purpose | Key request fields | Errors |
|---|---|---|---|
| `GET /sub-agents` | List sub-agents (common filters). | — | — |
| `POST /sub-agents` | Create a custom sub-agent (201). The workflow is structurally validated **and** factory-compiled at save time. | `name`, `description`, `persona`, `model`, `model_params`, `workflow` ([workflow-dsl.md](workflow-dsl.md)) | 422: workflow validation/compile errors (joined string); invalid model fields |
| `POST /sub-agents/check-overlap` | Advisory pre-save duplicate check. | `name`, `description`, `skill_ids`, `exclude_id` | — |
| `GET /sub-agents/{agent_id}` | One sub-agent (embeds `skills`, `workflow`). | — | 404 |
| `PATCH /sub-agents/{agent_id}` | Partial update; workflow changes re-validate and re-resolve skills. | `SubAgentPatch` (adds `status`) | 403 static guard; 403 `workflow`/`persona` on `kind: "native"`; 422 as on create |
| `POST /sub-agents/{agent_id}/validate` | Dry-run factory compile. | — | 404. Returns `{valid, errors: []}` (native agents are always `valid: true`) |
| `DELETE /sub-agents/{agent_id}` | Soft-delete. | — | 403 static |

## MCP servers router — `backend/app/api/mcp_servers.py` (prefix `/mcp-servers`)

| Method + path | Purpose | Key request fields | Errors |
|---|---|---|---|
| `GET /mcp-servers` | List servers; each row carries `tool_count`. | — | — |
| `POST /mcp-servers` | Register a server (201) as `status: "inactive"`; the MCP manager connects it asynchronously and flips status to `active`/`error`. | `name`, `description`, `transport` (`stdio`\|`http`), `command`+`args`+`env` (stdio), `url`+`headers` (http) | 422: `stdio` without `command`, `http` without `url` |
| `GET /mcp-servers/{server_id}` | One server. | — | 404 |
| `PATCH /mcp-servers/{server_id}` | Edit definition or toggle `status`. | `McpServerPatch` | 403 static guard |
| `DELETE /mcp-servers/{server_id}` | Soft-delete server **and its ingested tools**; disconnects. | — | 403 static; 409 if its tools are bound to skills |
| `POST /mcp-servers/{server_id}/reconnect` | Force a reconnect attempt. | — | 404; 503 manager not running |
| `POST /mcp-servers/{server_id}/refresh-tools` | Re-ingest the server's tool list. | — | 404; 503 manager not running |

`McpServerOut` adds `transport`, `command`, `args`, `env`, `url`, `headers`, `last_connected_at`, `last_error`, `tool_count`.

## Chat router — `backend/app/api/chat.py`

| Method + path | Purpose | Key request/response fields | Errors |
|---|---|---|---|
| `GET /conversations` | List conversations newest-first: `[{id, title, created_at, updated_at, run_count}]`. | — | — |
| `POST /conversations` | Create a conversation (201): `{id, title}`. | `{title?}` | — |
| `GET /conversations/{id}` | Conversation detail: `{id, title, messages, runs}`. `messages` interleaves `user` / `assistant` / `error` roles (failed and cancelled runs keep their error in place); assistant messages carry `answer_ui`. | — | 404 |
| `POST /chat` | Start a run (201). Omitting `conversation_id` creates a conversation from the message. The run executes as an asyncio task in-process. | Request `{conversation_id?, message}` → `{run_id, conversation_id}` | 422 empty message |
| `GET /chat/stream/{run_id}` | SSE event stream: full history replay, then live events. See [sse-events.md](sse-events.md). | — | 404 run not found |
| `POST /runs/{run_id}/hitl` | Resolve the pending HITL gate and resume from checkpoint. | `{decision: "approve"\|"deny", note?, answers?: {question_id: value}}` → `{status: "resuming", decision}` | **409 if the run is not `paused_hitl`** (`"run is <status>, not paused_hitl"`) |

## Runs router — `backend/app/api/runs.py` (prefix `/runs`)

| Method + path | Purpose | Errors |
|---|---|---|
| `GET /runs` | All runs, newest-first. Each: `{id, conversation_id, chat_message, status, orchestrator_mode, plan, snapshot, final_answer, answer_ui, error, started_at, finished_at, total_input_tokens, total_output_tokens}`. | — |
| `GET /runs/{run_id}` | Run detail including ordered `steps`: `[{id, parent_step_id, sub_agent_id, node_id, step_type, input, output, model, input_tokens, output_tokens, status, started_at, finished_at, error}]`. | 404 |
| `POST /runs/{run_id}/cancel` | Cooperative cancel; cancelling a `paused_hitl` run resolves it as `cancelled`. Returns `{status: "cancelled"}`. | 409 unless the run is `running` or `paused_hitl` |
| `POST /runs/{run_id}/retry` | Re-plan a **failed** run from its original message (201): `{run_id, conversation_id}` for the new run. | 409 `"only failed runs can be retried"` |
| `DELETE /runs/{run_id}` | Hard-delete one run and its steps; also drops its SSE history. | 404; 409 if `running` (`"cancel the run before deleting it"`) |
| `DELETE /runs` | Purge all run history (runs + steps + SSE event buffers). 204. | — |

Run `status` values: `running`, `paused_hitl`, `completed`, `failed`, `cancelled`.

## Cache router — `backend/app/api/cache.py` (prefix `/cache`)

| Method + path | Purpose | Errors |
|---|---|---|
| `GET /cache/status` | Registry cache status: `{mode: "bypass"\|"memory"\|"redis", registries: {tools\|skills\|sub_agents\|settings: {records, generation, loaded_at, cached}}}`. In `bypass` mode `records`/`loaded_at` are `null` and `cached` is `false`. | — |
| `POST /cache/refresh/{registry}` | Operator-forced eager reload of one registry (`tools`, `skills`, `sub_agents`, `settings`) or `all`. | 422 `unknown registry '<x>'; one of [...]/all` |

## Seed router — `backend/app/api/seed.py`

| Method + path | Purpose |
|---|---|
| `POST /seed/reload` | Idempotent re-run of the startup seed (native tools, `.skill.md` skills, seeded sub-agents), then invalidates every cache registry. Returns `{status: "ok", ...summary counts}`. |

## Fake-LLM router — `backend/app/api/fake_llm.py` (prefix `/_fake`, flag-gated)

Demo/testing only. Every endpoint returns **404** unless the `FAKE_LLM_ENABLED` environment variable is set (`backend/app/config.py`) — the router is invisible in normal deployments. Lets an external driver queue deterministic fake-model responses for keyless demos and the acceptance walk.

| Method + path | Purpose | Request | Response |
|---|---|---|---|
| `POST /_fake/script` | Queue scripted model responses in order. | `{calls: [{content?, tool_calls?, error?, delay_s?}]}` — `error` queues a raised `RuntimeError`; otherwise an AI message with optional tool calls and delay. | `{queued, pending}` |
| `POST /_fake/clear` | Drop any queued script. | — | `{pending: 0}` |

## Root endpoints — `backend/app/main.py` (no `/api/v1` prefix)

| Method + path | Purpose |
|---|---|
| `GET /health` | Liveness: `{"status": "ok"}`. |
| `GET /metrics` | Prometheus metrics (runs/steps/tokens/errors counters and histograms, `backend/app/obs.py`), `text/plain; version=0.0.4`. |
