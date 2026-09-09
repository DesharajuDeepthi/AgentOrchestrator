# Current State — What A2A Reuses, Subsystem by Subsystem

An eight-reader parallel audit of the codebase (2026-08-27, branch
`a2a_xperiment` off the merged memory+ambient `dev`) mapped every subsystem
the A2A wave must plug into. This file records the reuse inventory — what
exists, what the new code copies, and the exact seams. File:line references
are to the audit commit.

## 1. MCP server manager — the template for "remote capability provider"

`McpServer` (RegistryRecord subclass) + `McpManager` singleton
(`backend/app/mcp/manager.py`) is the exact prior art for a registry of
external capability providers: register from UI → connect → ingest
capabilities into the `tools` registry → health loop → status/last_error
bookkeeping → soft-delete cascades to projected tools. A2A copies the shape
but is **simpler**: no persistent sessions (HTTP is stateless), so "health"
is a periodic card re-fetch + skill re-ingest instead of ping-or-teardown.
Reused patterns verbatim:

- SDK isolation: the `mcp` SDK is imported only inside `app/mcp/manager.py`;
  `a2a-sdk` will be imported only inside `app/a2a/`.
- Ingest upsert: `tool_key = f'{server.name}.{name}'` with 6-hex collision
  suffix, refresh-in-place for existing rows, vanished entries flip
  `status='inactive'` (never deleted, ids stable), ends with
  `get_cache().invalidate('tools')` (`manager.py:177-231`).
- API: POST creates row then connects inline so the 201 already shows
  active/error; DELETE soft-deletes server AND projected tools after a 409
  dependents check on bound skills (`api/mcp_servers.py:103-129`).
- Module singleton `get_manager()/set_manager()` started from lifespan.
- `mcp_servers` is NOT in the registry cache (only tools/skills/sub_agents/
  settings are) — `remote_agents` follows suit.

## 2. Tools runtime — one dispatch point, lazy proxies

Tools are never statically bound: every loop is
`create_agent(model, tools=[], middleware=build_middleware_stack(ctx))` and
`ToolsRegistryMiddleware.awrap_model_call` injects materialized tools from a
fresh cache read per model call. `materialize_tool(row)`
(`factory/worker.py:234-237`) is the **only** switch on `Tool.kind`
(`'mcp' → _make_mcp_proxy`, else `_make_native_tool`) — the `a2a` branch
lands there and covers scoped, exposed, and full-catalog paths at once.
The MCP proxy pattern to copy: `StructuredTool(name=sanitize_tool_name(
tool_key), description, args_schema=<JSON-schema dict>,
coroutine=lazy_call)` where the backend (live manager/agent) is resolved
**inside the coroutine at call time** — dead backend = tool-call error
(error-edge semantics), recovered backend = zero rebuild.

The audit's exhaustive touch-list for a new tool source kind:
`materialize_tool` branch; an ingestion path writing rows with the new kind
+ cache invalidate; `models/tool.py` (new nullable FK `remote_agent_id` —
migration) + `registry_cache._tool_record` + `worker.snapshot_skill` +
`_skill_record` projections; `schemas/tool.py` kind Literal;
`ladder.py:306`'s hard-coded `kind='mcp'` on direct-tool steps (pre-existing
mislabel — fix to use the record's kind); frontend `types.ts` kind union +
ToolsPage kind filter + KindBadge tone; the singleton-manager lifecycle;
`retrieval.py` embeddable-text branch. Nothing in the middlewares, planner,
ladder rung-1, or obs needs changing — they consume the record dict +
`materialize_tool` generically.

Hard constraint honored: no fourth middleware — A2A plugs into
`materialize_tool`, not the middleware stack (CLAUDE.md / spec §7.0).

## 3. Runs + HITL — tool-level interrupts have precedent

HITL is LangGraph `interrupt()` end to end; `GraphInterrupt` is explicitly
re-raised through every generic except (middleware `awrap_tool_call`,
`ladder.execute_resolution`, worker error handling) — a tool coroutine MAY
interrupt. The precedent is agentic mode's `spin_worker` tool: resume
replays the interrupted tool call before any model call, so interrupting
tools must be **replay-idempotent** — the codebase's established fix is
adopt-don't-recreate (`ladder.find_running_dispatch` adopts the open
dispatch step; `invoke_worker_with_hitl` burns already-answered interrupts).
The A2A proxy copies this: an `a2a_tasks` row keyed by (run, call_key)
lets a replayed call adopt the open remote task instead of re-sending.
The `hitl_request` SSE payload contract ({prompt, node_id, questions?,
step_id}) and the `HitlCard` text-question renderer are reused unchanged —
a remote agent's `input-required` question becomes a text-kind form gate.

Cancellation is cooperative asyncio (`task.cancel()` →
`CancelledError` through the graph) — the A2A proxy catches
`CancelledError`, best-effort `tasks/cancel` to the remote, re-raises.

## 4. Settings + cache — one DEFAULTS entry per key, dark-by-default recipe

New settings keys are DEFAULTS entries + a validator classification —
no migration (`settings_store.py`). The dark-by-default DB-flag recipe
(`ambient_enabled` precedent): default False; every entry point checks
`await get_cache().setting('a2a_enabled')`; mutating APIs 409 naming the
flag; reads stay 200; tools/proxies return soft error results; background
work re-checks per tick; byte-identity proven by a dedicated test. Registry
write paths end with `invalidate('tools')` after commit. `remote_agents`
does NOT join the four cached registries (manager-held state + direct
reads, like `mcp_servers`); its projected tools ride the cached `tools`
registry automatically. With auth on, the new router must join
`auth._ADMIN_WRITE`.

## 5. Ambient — the long-running substrate to ride

Verbatim-reusable pieces for parked A2A tasks: the leader-tick evaluator
slot in `run_ambient_loop` (new evaluators go inside the leader branch or
run N times per replica set); `add_delivery` as the single outbox path
(category-tier rules, skey supersede-collapse, quiet hours/digest flushing
all free); the `hitl_aged` precedent of "parked thing ages into a delivery
with NO run"; the fence block + fixed untrusted-payload paragraph from
`prompts/ambient_run.md:13-17`. Hard findings that shaped the design:
`AmbientWakeup` is routine-addressed only — so parked A2A tasks do NOT use
wakeups; they get their own poller evaluator (the `poll_due_intents` shape).
There is no "parked" run status and `_supervise`'s 900s wall clock kills
in-place waiters — so parking **ends the tool call with a structured
result** and hands the task to the ambient poller; it never holds the run
open. Ambient dark ⇒ no poller ⇒ parking disabled (in-run timeout becomes
a plain tool error) — documented, deliberate.

## 6. Models + migrations + docs conventions

RegistryRecord base gives id/name/description/source/status/soft-delete;
JSONB via `Base.type_annotation_map`; hand-authored sequential Alembic
migrations; Pydantic v2 schemas separate from ORM. Spec conventions (from
the docs mapper, exact insertion points recorded in doc 06): new `## 19.`
section appends at end of spec.md; M37–M39 rows join the headerless M13+
milestone block; settings keys go inline in the §3.7 paragraph under a
"plus the §19 keys:" group; acceptance steps are a bold `**§14d …**`
paragraph with global numbering continuing at 33; the Admin-UI page is
`### 8.10`; README milestone rows + status blockquote + a feature
paragraph follow completion.
