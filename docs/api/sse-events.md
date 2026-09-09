# SSE Event Stream Contract

The chat/run event stream is how the frontend renders a live run: plan cards, route lines, dispatch rails, token streaming, HITL approval cards, and the final answer UI. This document is the contract implemented by `backend/app/orchestrator/recorder.py`, `backend/app/orchestrator/runner.py`, `backend/app/orchestrator/graph_mode.py`, `backend/app/orchestrator/ladder.py`, and `backend/app/api/chat.py`, and consumed by `frontend/src/api/client.ts` (`streamRun`) and `frontend/src/pages/ChatPage.tsx`.

Related: [REST API](rest-api.md) (how runs are started and controlled) · [Workflow DSL](workflow-dsl.md) (where HITL gates come from).

## Connecting

```
GET /api/v1/chat/stream/{run_id}        Accept: text/event-stream
```

- 404 if the run does not exist.
- Each SSE message uses the event's `type` as the SSE **event name** and a JSON **envelope** as data:

  ```json
  {"type": "token", "run_id": "<uuid>", "ts": "<ISO-8601 UTC>", "payload": { ... }}
  ```

  The frontend registers one listener per known event name on an `EventSource` (`frontend/src/api/client.ts`).

### Replay, reconnect, and stream end

- **Replay**: on connect, the server first replays the run's **entire event history**, then switches to live delivery (`RunEventBus.subscribe`, `backend/app/orchestrator/context.py`). Reconnecting mid-run (or re-opening a conversation with an in-flight or paused run) replays everything from the beginning — the client must rebuild its view from scratch per connection, which `ChatPage.tsx` does by resetting all live state when it (re)attaches to a `run_id`.
- History is **in-memory, per process**. It survives for the life of the backend process (or until `DELETE /runs`/`DELETE /runs/{id}` forgets it); after a backend restart a stream opens with an empty history.
- **Stream end**: the server closes the stream after a terminal event — a `done` event, or a `run_status` with status `failed` or `cancelled` (`_is_terminal`, `backend/app/api/chat.py`). `run_status: completed` is always followed by `done`, so `done` is the practical end-of-stream marker for successful runs.
- **`paused_hitl` is not terminal**: the stream stays open across an HITL pause; resume events arrive on the same connection.
- **Keepalive**: after 120 s with no events the server emits a `ping` event with data `{}` (not enveloped) and keeps waiting.
- `EventSource` auto-reconnects on network errors; because of full-history replay this is safe and idempotent as long as the client rebuilds state per connection.

## Event catalog

Every event type actually emitted by the backend (grep: `recorder.emit(` / `ctx.recorder.emit(`), cross-checked against the listener list in `frontend/src/api/client.ts`.

### `run_status`

Run lifecycle transitions. Emitted at start (`running`), on pause (`paused_hitl`), on each resume (`running` again), and at the end (`completed` / `failed` / `cancelled`). Source: `backend/app/orchestrator/runner.py`.

| Field | Type | Meaning |
|---|---|---|
| `status` | string | `running` \| `paused_hitl` \| `completed` \| `failed` \| `cancelled` |

### `plan`

The orchestrator's plan. Graph mode emits it once after the planner step; agentic mode re-emits it every time the middleware todo list changes (updates supersede — render only the latest).

| Field | Type | Meaning |
|---|---|---|
| `entries` | array | Graph mode only: `[{id, capability: {type, id?, skill_ids?}, task, depends_on: [id]}]` |
| `todos` | array | Agentic mode only: `[{content, status}]` (`status`: `pending`/`in_progress`/`completed`) |
| `mode` | string | `"graph"` or `"agentic"` |

### `route`

One deterministic ladder resolution (spec §7.1). Emitted per plan entry in graph mode, and when the full-catalog fallback engages. Source: `RunRecorder.record_route` and `fallback_node`.

| Field | Type | Meaning |
|---|---|---|
| `capability` | object | The plan entry's capability, e.g. `{type: "direct_skill", id: "<uuid>"}`; `{type: "full_catalog"}` for fallback |
| `rung` | string | `direct_tool` \| `direct_skill` \| `native_sub_agent` \| `custom_sub_agent` \| `dynamic_worker` \| `fallback` |
| `resolved_to` | object | `{rung, entity_id, entity_name, tier, kind}` (`Resolution.as_route`, `backend/app/orchestrator/ladder.py`); fallback uses `{entity_id: null, entity_name: "full-catalog fallback"}` |

### `dispatch_start` / `dispatch_end`

Brackets around a dispatched unit of work (a sub-agent worker, ephemeral worker, direct tool/skill execution, or the fallback loop). Emitted by `RunRecorder.start_step`/`finish_step` when called with `emit_dispatch=True`. The frontend renders one "rail" per `dispatch_start` and nests child steps under it.

| Field | Type | Meaning |
|---|---|---|
| `step_id` | string | The dispatch step's `run_steps` id — correlates start/end and parents `activity`/`hitl_request` events |
| `tier` | string | Label tier, e.g. `sub_agent`, `tool`, `skill`, `orchestrator` |
| `kind` | string \| null | `native` \| `custom` \| `dynamic` \| `mcp` \| `fallback` … |
| `entity_id` | string \| null | Registry id of the dispatched entity (null for ephemeral workers) |
| `entity_name` | string | Display name (ephemeral workers: `worker-alpha (web-research+file-ops)`) |
| `status` | string | **`dispatch_end` only**: `completed` \| `failed` \| `cancelled` |

### `activity`

The live step ticker: every run-step transition, without payloads. Emitted on every `start_step` (status `running`, full identity) and every `finish_step` (status only). `step_type` values: `plan`, `route`, `skill`, `hitl`, `tool_call`, `aggregate`.

| Field | Type | Meaning |
|---|---|---|
| `step_id` | string | Run-step id |
| `parent_step_id` | string \| null | **Start only.** Owning dispatch step — lets the client nest child lines under the right rail |
| `step_type` | string | **Start only.** See list above |
| `tier` | string | **Start only.** |
| `kind` | string \| null | **Start only.** |
| `entity_name` | string \| null | **Start only.** Skill/tool/agent display name |
| `node_id` | string \| null | **Start only.** Workflow node id, when inside a sub-agent DAG |
| `status` | string | `running` on start; `completed`/`failed`/`cancelled` on finish |

### `token`

A streamed chunk of the final answer text (aggregator streaming in graph mode; the whole final answer as one chunk in agentic mode and for planner `direct_answer`s). Concatenate in arrival order.

| Field | Type | Meaning |
|---|---|---|
| `text` | string | Text delta |

### `thinking`

Model reasoning deltas from the aggregator stream (emitted only when the provider surfaces thinking content). Rendered as a collapsible "model thinking" block.

| Field | Type | Meaning |
|---|---|---|
| `text` | string | Thinking delta |

### `hitl_request`

A human gate is waiting. Emitted by the worker executor just before the run pauses (`backend/app/orchestrator/ladder.py`), and re-announced by the runner after a resume that pauses again on a *different* gate (`_emit_pending_hitl`, `backend/app/orchestrator/runner.py`). Resolve it with `POST /api/v1/runs/{run_id}/hitl` ([rest-api.md](rest-api.md)); a `run_status: paused_hitl` follows shortly after.

| Field | Type | Meaning |
|---|---|---|
| `prompt` | string | The gate's question, from the workflow `hitl` node |
| `node_id` | string | The workflow node id of the gate |
| `questions` | array \| null | Form gate only: `[{id, prompt?, kind: "approve"\|"choice"\|"text", options?}]` — see [workflow-dsl.md](workflow-dsl.md). Present on the original emission; runner re-announcements carry `prompt`/`node_id`/`step_id` only |
| `step_id` | string \| null | The owning **dispatch** step id — the client nests the approval card inside that rail; `null` for ownerless gates |

### `answer_ui`

The formatter's structured artifact, generated after a successful run when `formatter_enabled` is on (`backend/app/orchestrator/answer_ui.py`). The payload carries `presentation` ('a2ui_first'|'raw_first', frozen at run time) and `coverage` (deterministic content-retention percent). Failure-safe: absent whenever generation fails or produces nothing — the streamed `token` text is always the source of truth, and with no artifact the UI shows no structured toggle at all. A separate `charts {charts: [..]}` event carries chart specs produced by the `render_chart` tool during the run — emitted whenever present, independent of the formatter.

| Field | Type | Meaning |
|---|---|---|
| `a2ui` | array | Two A2UI **v0.9** protocol messages: `createSurface` (surface `"answer"`, basic catalog) + `updateComponents` with the flattened component list |
| `charts` | array (optional) | Only when `answer_ui_charts_enabled` and the model emitted chart components: `[{kind: "bar"\|"line"\|"pie", title, labels: [string], series: [{name, values: [number]}]}]` — rendered by the app's own themed SVG chart, not A2UI |

The same payload persists on the run as `answer_ui` and is returned by `GET /conversations/{id}` and `GET /runs/{id}` for reloads.

### `error`

A failure surface. Two shapes from two sources:

| Field | Type | Meaning |
|---|---|---|
| `message` | string | Human-readable error |
| `step_id` | string | Present only for step-level failures (`finish_step(status="failed")`); absent on run-level failure (emitted just before `run_status: failed`) |

### `done`

Terminal success marker; the server closes the stream after sending it.

| Field | Type | Meaning |
|---|---|---|
| `answer` | string | The complete final answer |
| `tokens` | object | `{input_tokens, output_tokens}` run totals |

### `ping`

Keepalive after 120 s of silence. Data is literal `{}` — no envelope. Ignore it.

## Ordering guarantees

- Events are emitted into a per-run FIFO (`RunEventBus`); subscribers receive history then live events **in emission order**. There is no cross-run ordering.
- A run always begins with `run_status: running`. `plan` precedes `route`; each `route` precedes its entry's `dispatch_start`; `dispatch_end` follows all of that dispatch's child `activity` events.
- `hitl_request` always precedes its `run_status: paused_hitl`. After `POST /hitl`, the next events are `run_status: running`, then either further progress or (multi-gate runs) another `hitl_request` + `run_status: paused_hitl`.
- On success the tail is: [`answer_ui`] → `run_status: completed` → `done`. On failure: `error` → `run_status: failed`. On cancel: `run_status: cancelled` (no `error`).

## Client guidance

- **Treat every connection as a full replay.** Reset all derived state when (re)subscribing to a run, then fold events in order — this makes reconnects and mid-run re-attachment idempotent for free (`LiveRun` in `frontend/src/pages/ChatPage.tsx` does exactly this).
- **Latest-wins events**: render only the newest `plan` (agentic todo updates supersede) and the newest `hitl_request`.
- **The paused_hitl → resolution → run_status pattern (stale-card fix)**: a gate can be resolved from surfaces other than your card — the Settings HITL queue (`GET /hitl/pending`), a cancel, or a resume replay. Never leave an armed approval card based only on your own click. The rule implemented in `ChatPage.tsx` (and verified by `docs/acceptance/22-hitl-stale-card-fix/`): **any `run_status` event arriving *after* the newest `hitl_request` whose status is not `paused_hitl` means that gate has been consumed** — collapse the card to its resolved state. Additionally, a 409 from `POST /runs/{run_id}/hitl` means the run is no longer paused: treat the gate as consumed rather than showing an error.
- **Correlate by `step_id`**: `activity.parent_step_id` and `hitl_request.step_id` point at a `dispatch_start.step_id`, letting you nest child steps and approval cards under the dispatch rail that owns them.
