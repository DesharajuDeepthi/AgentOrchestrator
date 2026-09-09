# State Machines

Lifecycle diagrams for the three stateful objects that drive the runtime: runs, MCP server connections, and the frontend HITL gate card. Every state name below is a literal value from the code cited in each section.

---

## 1. Run status lifecycle

Statuses are stored on `Run.status` (`backend/app/models/run.py`) and written exclusively by the runner (`backend/app/orchestrator/runner.py`) and its control operations. The literal values in code: **`running`**, **`paused_hitl`**, **`completed`**, **`failed`**, **`cancelled`**.

```mermaid
stateDiagram-v2
    [*] --> running : "POST /chat — create_run inserts status running, start_run_task spawns _execute"
    running --> paused_hitl : "worker interrupt() propagates — _execute sees __interrupt__"
    paused_hitl --> running : "POST /runs/{id}/hitl — resume_run + start_run_task(resume)"
    running --> completed : "answer produced — final_answer, answer_ui persisted"
    running --> failed : "RunFailed or unhandled exception — _finalize_failure"
    running --> cancelled : "POST /runs/{id}/cancel — task.cancel(), CancelledError path"
    paused_hitl --> cancelled : "cancel while paused — _finalize_failure('cancelled while paused')"
    failed --> [*] : "POST /runs/{id}/retry creates a NEW run (409 for any other status)"
    completed --> [*]
    cancelled --> [*]
```

**Notes grounded in code:**

- There is no `queued` or `pending` state: `create_run` inserts the row already at `running`, and the asyncio task starts immediately. The "queued message" affordance in the chat composer is **frontend-only state** (`queuedDraft` in `frontend/src/pages/ChatPage.tsx`): the draft is held in React state, bound to its conversation, and only POSTed to `/chat` after the conversation's active run leaves `running`/`paused_hitl`. It never touches backend run state.
- Every status transition is mirrored to SSE as a `run_status` event; `failed` and `cancelled` are terminal for the stream (`_is_terminal` in `backend/app/api/chat.py`), while `completed` is followed by the terminal `done` event.
- **Failure path** (`_finalize_failure`): sets `status` + `error` + `finished_at`, flips any still-`running` `RunStep` rows to `cancelled`, emits an `error` SSE event (for `failed` only) then `run_status`.
- **Retry is not a transition**: `retry_run` refuses anything but `status == "failed"` (409 at the API) and creates a brand-new run re-planned from the original `chat_message` — the failed run keeps its status forever.
- **Cancellation** is cooperative: `cancel_run` cancels the live asyncio task from `RUNNING_TASKS` (→ `CancelledError` → `_finalize_failure(..., "cancelled", ...)`). If no task is live (a paused run after e.g. a restart), it finalizes directly; only `running`/`paused_hitl` runs can be cancelled (409 otherwise).
- **Resume that pauses again**: a resumed run goes `paused_hitl → running → paused_hitl` when parallel gates remain; `_emit_pending_hitl` re-announces the surviving gates so the UI shows the next card.
- `RunStep.status` uses the same vocabulary minus `paused_hitl`: `running`, `completed`, `failed`, `cancelled` (`RunRecorder.finish_step`, `backend/app/orchestrator/recorder.py`).

---

## 2. MCP server health and connection states

Stored on `McpServer.status` and written by `McpManager._record_status` (`backend/app/mcp/manager.py`) and the registry API (`backend/app/api/mcp_servers.py`). The literal values: **`inactive`**, **`active`**, **`error`**, plus soft deletion via `deleted_at`.

```mermaid
stateDiagram-v2
    [*] --> inactive : "POST /mcp-servers — row created, source dynamic, not yet connected"
    inactive --> active : "connect_server ok — session initialized, tools ingested, last_connected_at set"
    inactive --> error : "connect failed — timeout / spawn / HTTP error recorded as last_error"
    active --> error : "health ping failed — ping_all teardown, last_error 'health ping failed'"
    active --> error : "reconnect attempt failed"
    error --> active : "POST /mcp-servers/{id}/reconnect — full connect + re-ingest"
    error --> error : "reconnect failed again"
    active --> inactive : "PATCH status toggle — UI Deactivate (registry flag only)"
    inactive --> active : "PATCH status toggle back, or reconnect"
    active --> deleted : "DELETE — soft delete + disconnect (409 while tools bound to skills)"
    inactive --> deleted : "DELETE"
    error --> deleted : "DELETE"
    deleted --> [*]
```

**Notes grounded in code:**

- The connection itself is a separate in-memory object (`_Connection`: an asyncio task holding the client context open, a `ready` event, a `stop_event`). `connect_server` waits up to `CONNECT_TIMEOUT_S = 25.0` for readiness, then ingests tools; any failure in that window tears down and records `error` with a human-readable `last_error` via `_describe` (timeouts become "connection timed out", exception groups are deduplicated).
- **Startup**: `McpManager.start` connects every non-deleted server concurrently and starts the health loop — DB is the source of truth, so dynamic servers survive restarts (spec §5).
- **Health loop**: `_health_loop` sleeps `mcp_health_interval_s` (re-read from settings every cycle, so it is live-tunable) then `ping_all` sends `session.send_ping()` with `PING_TIMEOUT_S = 5.0`; failure → teardown + `error`.
- The `PATCH` status toggle (`Deactivate`/`Activate` in `frontend/src/pages/McpServersPage.tsx`) writes only the registry flag through `patch_server`; it does not itself tear down or establish the connection — `reconnect` and `delete` are the endpoints that manage the session.
- `_record_status(..., "active", connected=True)` is the only writer of `last_connected_at`.

---

## 3. HITL gate lifecycle (frontend)

The gate card is `HitlCard` inside `LiveRun` (`frontend/src/pages/ChatPage.tsx`). Its lifecycle is derived from three pieces of state: the component-local `busy` flag, the `hitlResolved` flag in `LiveRun`, and the event-derived `gateConsumed` predicate.

```mermaid
stateDiagram-v2
    [*] --> armed : "hitl_request SSE arrives — setHitlResolved(false) re-arms"
    armed --> armed : "form gate — answers filled, Submit disabled until allAnswered"
    armed --> busy : "Approve / Deny / Submit clicked — POST /runs/{id}/hitl"
    busy --> resolved_by_click : "2xx — onResolved() sets hitlResolved"
    busy --> collapsed_on_error : "409 — resolved elsewhere; catch handler calls onResolved() anyway"
    armed --> resolved_by_event : "gateConsumed — run_status after the newest hitl_request with status != paused_hitl"
    resolved_by_click --> [*] : "card shows 'resolved — resuming from checkpoint…'"
    resolved_by_event --> [*]
    collapsed_on_error --> [*]
    resolved_by_click --> armed : "next hitl_request re-arms a fresh gate"
```

**Notes grounded in code:**

- **Armed**: only the latest `hitl_request` renders (`lastHitlIdx`). If its payload carries a `step_id` matching a known dispatch rail (`railIds`), the card nests inside that `DispatchCard`; ownerless gates render at top level. Form gates (`questions` in the payload, spec §3.5) render text inputs and choice/approve chips; the Approve button is disabled until every question has an answer (`allAnswered`), and answers are sent only on `approve`.
- **Busy**: `decide()` sets `busy` to disable both buttons while the POST is in flight.
- **resolved_by_click**: a 2xx response calls `onResolved()`, which sets `hitlResolved` in `LiveRun`; the card swaps its controls for "resolved — resuming from checkpoint…".
- **resolved_by_event (`gateConsumed`)**: computed over the event log — `lastHitlIdx >= 0` and some later event is a `run_status` whose status is not `paused_hitl`. This covers gates resolved through the Settings HITL queue, a cancel, or a resume replay: armed buttons are never left on a gate the backend already closed. `gateResolved = hitlResolved || gateConsumed` is what the card actually receives.
- **collapsed_on_error**: the POST's catch handler treats failure (in practice the 409 from `resolve_hitl` when the run is no longer `paused_hitl`) as "this gate was resolved through another surface" and collapses rather than staying armed-but-dead.
- **Re-arm**: each incoming `hitl_request` event runs `setHitlResolved(false)` — a multi-gate run pauses again and presents the next gate as a fresh armed card.
- On conversation reopen, the re-attach effect finds a `running`/`paused_hitl` run and re-subscribes; the SSE history replay restores the card in the correct state.

---

**See also:** [runtime-flows.md](runtime-flows.md) · [resolution-ladder.md](resolution-ladder.md) · [overview.md](overview.md)
