# Architecture Overview

This document is the entry point to the Concierge Agent architecture. It describes the system as built — every claim below is grounded in the source under `backend/app/`, `frontend/src/`, and `docker-compose.yml`. `spec.md` at the repo root is the specification the code implements; where the two drift, this document describes the code and flags the difference.

## The system in 10 minutes

Concierge Agent is a proof of concept for **registry-driven agentic orchestration**: a chat frontend, a single-process FastAPI + LangGraph backend, and a Postgres database that holds three capability registries. A user types a request; an orchestrator plans against the live registries, resolves each plan entry to the cheapest capability that can serve it, executes — possibly in parallel, possibly pausing for human approval — and streams a fully labeled trace back over SSE.

The core idea is the **tri-layer registry model**:

- **Tools** (`backend/app/models/tool.py`) are atomic callables. They come from two sources: MCP servers plugged in at runtime (`kind='mcp'`, ingested by `backend/app/mcp/manager.py` from `tools/list`) and code-defined native functions (`kind='native'`, registered at startup by `backend/app/native/provider.py`).
- **Skills** (`backend/app/models/skill.py`) are markdown documents — YAML frontmatter (name, persona, bound tools, `direct_exposure`) plus a multi-step instruction body, parsed by `backend/app/skilldoc.py`. Native skills ship as `.skill.md` files under `backend/app/native/skills/`; custom skills are authored in the admin UI in the same format. A skill executes as a LangChain `create_agent` tool loop that sees *only* its bound tools — isolation is enforced structurally by the middleware stack, not by prompt.
- **Sub agents** (`backend/app/models/sub_agent.py`) compose skills into workflows. Custom sub agents carry a JSON DAG (branching, parallel fan-out/join, error edges, human-in-the-loop gates) validated at save time by `backend/app/factory/dag.py` and compiled into a LangGraph `StateGraph` by the worker factory in `backend/app/factory/worker.py`. Native sub agents are code-built subgraphs registered with `covers_skill_ids`.

Every registry row is either `static` (seeded, definition immutable — only `status` and `direct_exposure` can be toggled) or `dynamic` (created at runtime through the API/UI). Registry state is projected into running agents *live*: three custom registry middlewares (`backend/app/orchestrator/middleware.py`) re-read the registries at each model call, so an MCP tool plugged in mid-conversation is callable on the next loop iteration without a restart.

There are **two orchestrator modes**, switchable per run via the `orchestrator_mode` setting:

- **Graph mode** (`backend/app/orchestrator/graph_mode.py`) is a hand-built `StateGraph`: `plan → resolve → dispatch (parallel, via Send) → aggregate`. The planner (`backend/app/orchestrator/planner.py`) emits structured output against compact registry summaries (progressive disclosure) with a validate → repair-once → fail contract. Each plan entry then walks the deterministic **resolution ladder** (`backend/app/orchestrator/ladder.py`): direct exposed tool / direct exposed skill → native sub agent covering the skill → custom sub agent using the skill → ephemeral dynamic worker composed on the fly.
- **Agentic mode** (`backend/app/orchestrator/agentic_mode.py`) is a single `create_agent` concierge loop. Planning is emergent via LangChain's `TodoListMiddleware`; capabilities are exactly the live registries projected by the three registry middlewares; a `spin_worker` tool covers ladder rung 4 and a `use_full_catalog` tool is the logged escalation that unlocks the entire registry mid-run.

Both modes share the same registries, the same executor (`execute_resolution` in `ladder.py`), the same run recorder, and the same label set — they are built to be A/B compared.

Everything else is supporting machinery: a `ModelProvider` port so no provider SDK leaks past `backend/app/llm/`, a registry cache facade with three switchable backends, an in-memory SSE event bus, a Postgres checkpointer for HITL pause/resume, and always-on run tracing (Postgres rows + structlog + Prometheus + optional OTel and LangSmith).

## System context (C4 level 1)

Actors and external systems, as wired in the code. Provider adapters live in `backend/app/llm/adapters.py`; MCP client transports in `backend/app/mcp/manager.py`; observability exporters in `backend/app/obs.py`.

```mermaid
graph TB
  user["User — chats, approves HITL gates"]
  admin["Admin — manages registries, MCP servers, settings"]

  subgraph system["Concierge Agent"]
    app["Registry-driven orchestration system<br>React admin UI + FastAPI/LangGraph backend + Postgres"]
  end

  anthropic["Anthropic API"]
  google["Google Gemini API"]
  openai["OpenAI API"]
  mcpstdio["MCP servers — stdio<br>(subprocesses, e.g. fetch, filesystem)"]
  mcphttp["MCP servers — streamable HTTP<br>(remote endpoints)"]
  langsmith["LangSmith (optional)<br>local or remote endpoint"]
  otel["OTel collector (optional)"]

  user -->|"HTTP/JSON + SSE"| app
  admin -->|"HTTP/JSON"| app
  app -->|"chat + embeddings calls (HTTPS)"| anthropic
  app -->|"chat + embeddings calls (HTTPS)"| google
  app -->|"chat + embeddings calls (HTTPS)"| openai
  app -->|"JSON-RPC over stdio"| mcpstdio
  app -->|"JSON-RPC over streamable HTTP"| mcphttp
  app -->|"run traces (HTTPS)"| langsmith
  app -->|"OTLP/HTTP spans"| otel
```

Provider API keys enter only as environment variables (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`) read by `backend/app/config.py` — never stored in the database or exposed through the UI. A key's presence is what enables its provider in the Settings model selects (`is_configured()` per adapter). A scriptable fake provider (`backend/app/llm/fake.py`, `fake:scripted`, gated by `FAKE_LLM_ENABLED`) makes the whole stack runnable with no keys at all.

## Containers (C4 level 2)

`docker-compose.yml` defines four services; `redis` sits behind a compose profile and does not run by default.

```mermaid
graph TB
  browser["Browser<br>React 19 + Vite + TanStack Query"]

  subgraph compose["docker-compose"]
    frontend["frontend<br>nginx 1.27 serving the Vite build<br>proxies /api and /metrics"]
    backend["backend<br>FastAPI, one asyncio process<br>LangGraph orchestration, MCP manager, run tasks"]
    db[("db<br>Postgres 16<br>registries, runs, settings, checkpoints")]
    redis[("redis<br>redis 7 — optional, profile 'redis'<br>registry cache backend only")]
    mcp1["MCP stdio subprocesses<br>spawned inside the backend container"]
  end

  anthropic["LLM provider APIs"]
  mcphttp["Remote MCP servers"]

  browser -->|"HTTP/JSON + SSE"| frontend
  frontend -->|"proxy_pass, buffering off for SSE"| backend
  backend -->|"asyncpg (SQLAlchemy) + psycopg pool (checkpointer)"| db
  backend -.->|"redis protocol, only when registry_cache_mode=redis"| redis
  backend -->|"stdio pipes"| mcp1
  backend -->|"streamable HTTP"| mcphttp
  backend -->|"HTTPS"| anthropic
```

Container notes, from the code:

- **frontend** — a multi-stage Dockerfile (`frontend/Dockerfile`) builds the Vite app and serves it with nginx. `frontend/nginx.conf` proxies `/api/` to `backend:8000` with `proxy_buffering off` and a one-hour read timeout so SSE streams survive, and proxies `/metrics` for Prometheus scrapes. The UI is seven pages (`frontend/src/pages/`): Chat, MCP Servers, Tools, Skills, Sub Agents, Runs, Settings.
- **backend** — one FastAPI process (`backend/app/main.py`). Runs are `asyncio.Task`s in the same process (`start_run_task` in `backend/app/orchestrator/runner.py`); there is no broker, queue, or worker pool. Startup lifecycle: Alembic `upgrade head` → seed load → checkpointer table setup → registry cache warm-up → MCP manager reconnect (non-blocking) → embedding backfill (non-blocking).
- **db** — the only mandatory stateful service. The backend opens two connection paths: an async SQLAlchemy engine over `asyncpg` (`backend/app/db.py:get_engine`) for application tables, and a `psycopg` `AsyncConnectionPool` for LangGraph's `AsyncPostgresSaver` checkpointer (`backend/app/db.py:get_checkpointer`), which creates its own checkpoint tables.
- **redis** — exists solely as an alternative backend for the registry cache (`backend/app/registry_cache.py`); enabled with `docker compose --profile redis up` plus `REDIS_URL`. Execution never depends on Redis.
- **MCP stdio subprocesses** are not compose services: `backend/app/mcp/manager.py` spawns them (`mcp.client.stdio.stdio_client`) inside the backend container, one long-lived asyncio task holding each client session open.

## Backend components (C4 level 3)

```mermaid
graph TB
  subgraph api["API layer — backend/app/api, mounted at /api/v1"]
    routers["Routers: chat, runs, tools, skills, sub_agents,<br>mcp_servers, settings, cache, seed, fake_llm"]
    sse["SSE endpoint<br>GET /chat/stream/{run_id}"]
  end

  subgraph orch["Orchestrator — backend/app/orchestrator"]
    runner["runner.py — run lifecycle,<br>asyncio tasks, HITL resume, cancel/retry"]
    graphmode["graph_mode.py — StateGraph<br>plan, resolve, dispatch, aggregate"]
    agenticmode["agentic_mode.py — create_agent concierge<br>spin_worker + use_full_catalog tools"]
    planner["planner.py — structured plan,<br>repair-once contract"]
    ladder["ladder.py — resolution ladder<br>+ shared executor"]
    mwstack["middleware.py — build_middleware_stack<br>3 registry projections + OOB middleware"]
    recorder["recorder.py — run_steps rows,<br>metrics, spans, SSE events"]
    bus["context.py — RunEventBus,<br>RunContext contextvar"]
  end

  subgraph factory["Worker factory — backend/app/factory"]
    dag["dag.py — DAG validation at save"]
    worker["worker.py — snapshot to CompiledStateGraph,<br>skill nodes as create_agent loops"]
  end

  subgraph llm["Provider layer — backend/app/llm"]
    port["port.py — ModelProvider protocol,<br>ModelParams, ModelInfo"]
    reg["registry.py — get_model, get_embeddings,<br>validate_model_selection"]
    adapters["adapters.py — anthropic, google_genai, openai<br>fake.py — fake:scripted"]
  end

  subgraph data["Data + platform"]
    cache["registry_cache.py — RegistryCache facade<br>bypass / memory / redis"]
    retrieval["retrieval.py — BM25 + cosine + RRF top-K"]
    settings["settings_store.py — app_settings defaults + validation"]
    mcpmgr["mcp/manager.py — connections, ingest,<br>listChanged, health loop"]
    native["native/provider.py — native tool and<br>sub agent registration"]
    dbmod["db.py — asyncpg engine +<br>psycopg checkpointer pool"]
    obs["obs.py — structlog, Prometheus,<br>OTel, LangSmith callbacks"]
    overlap["overlap.py — LLM-as-judge<br>duplicate check on save"]
  end

  routers --> runner
  routers --> cache
  routers --> mcpmgr
  routers --> settings
  routers --> dag
  routers --> overlap
  sse --> bus
  runner --> graphmode
  runner --> agenticmode
  graphmode --> planner
  graphmode --> ladder
  agenticmode --> ladder
  agenticmode --> mwstack
  ladder --> worker
  ladder --> mwstack
  worker --> mwstack
  mwstack --> cache
  planner --> cache
  planner --> retrieval
  ladder --> cache
  retrieval --> reg
  mwstack --> mcpmgr
  graphmode --> reg
  agenticmode --> reg
  worker --> reg
  reg --> port
  port --> adapters
  recorder --> bus
  recorder --> obs
  runner --> recorder
  runner --> dbmod
  cache --> dbmod
  native --> cache
```

Component responsibilities, grounded in source:

- **API routers** (`backend/app/api/__init__.py`) — REST/JSON under `/api/v1`: registry CRUD with static-record write rejection, MCP server lifecycle, chat + SSE, run control (cancel/retry/HITL), settings, cache mode/refresh, seed, and the `/_fake/script` control endpoint (mounted only when `FAKE_LLM_ENABLED` is set). `/health` and `/metrics` are on the app root.
- **Runner** (`orchestrator/runner.py`) — creates `runs` rows, launches each run as an `asyncio.Task` tracked in `RUNNING_TASKS`, dispatches to the mode implementations, finalizes status/tokens/answer, and handles HITL resume by replaying from the Postgres checkpoint (`Command(resume=...)`, targeting individual interrupts by id when parallel gates are pending). Cancellation is cooperative task cancellation.
- **Resolution ladder** (`orchestrator/ladder.py`) — `resolve_capability` walks the rungs deterministically over the registry cache: `direct_tool` / `direct_skill` → `native_sub_agent` (via `covers_skill_ids`) → `custom_sub_agent` (first custom agent using the skill) → `dynamic_worker` (ephemeral snapshot over **exposed** registry skills, gated by `dynamic_worker_fallback_enabled`, named `worker-alpha (skills...)` per run). `execute_resolution` is the single executor behind graph-mode dispatch and the agentic middlewares' handlers, including worker invocation with `interrupt()` propagation for HITL.
- **Worker factory** (`factory/worker.py`, `factory/dag.py`) — validates workflow DAGs at save (single START edge, acyclicity, reachable END, active skill refs, at most one error edge per node) and compiles snapshots into `CompiledStateGraph`s: explicit routing/fan-out/join mechanics at the shell, `create_agent` skill loops at the leaves, joins as LangGraph deferred nodes, `node_outputs` as an order-insensitive keyed merge.
- **Middleware stack builder** (`orchestrator/middleware.py`) — `build_middleware_stack(context)` is the only composition path. `SkillLoopContext` → Summarization + call limit + scoped `ToolsRegistryMiddleware` (bound tool ids only). `FallbackLoopContext` → full-catalog Tools + Skills projections. `AgenticLoopContext` → TodoList + Summarization + call limit + all three registry middlewares, exposure-gated, with a live `full_catalog` flag escalation. The three registry middlewares are stateless projections that re-resolve live tool/skill/sub-agent objects from the cache at every model call.
- **Provider port** (`llm/`) — `ModelProvider` is a `Protocol` (`port.py`); adapters self-register via the `@model_provider` decorator (`registry.py`); `get_model("provider:model")` and `get_embeddings(...)` are the only entry points. Adapters map the normalized `effort` param onto Anthropic thinking budgets (adaptive thinking for the Claude 5 family), Gemini thinking budgets, and OpenAI reasoning effort — OpenAI reasoning runs are routed through the Responses API (`use_responses_api=True` in `adapters.py`) because chat completions rejects function tools combined with `reasoning_effort`.
- **Registry cache** (`registry_cache.py`) — singleton facade over every registry/settings read in the run path. Backend chosen live by the `registry_cache_mode` setting: `bypass` (default: straight DB queries), `memory` (per-process, generation counters, reload-on-dirty), `redis` (same contract over Redis blobs). Freshness is event-driven: every write path calls `invalidate(registry)`; there are no TTLs. Cross-replica invalidation rides Postgres `LISTEN/NOTIFY` on channel `registry_cache_inv` — dormant in the single-node deployment.
- **Retrieval** (`retrieval.py`) — progressive-disclosure ranking, off by default (`retrieval_enabled=False`). Above `retrieval_threshold` records, catalogs are ranked to `retrieval_top_k` by Okapi BM25 over name/description plus cosine over stored embeddings (fetched through the embeddings port), fused with reciprocal-rank fusion. Entities already used in a run are pinned past ranking; skill loops and workers are never ranked — they stay id-pinned contracts.
- **MCP manager** (`mcp/manager.py`) — one asyncio task per active server holds the client session open (stdio via `stdio_client`, HTTP via `streamablehttp_client`); `tools/list` results upsert into the tools registry; `listChanged` notifications trigger reconciliation; a ping loop (interval from live settings) flips failing servers to `error`. The DB is the source of truth: startup reconnects every non-deleted server.
- **Settings store** (`settings_store.py`) — `app_settings` key/value rows with typed defaults (orchestrator mode, model refs and params, parallelism and iteration limits, cache mode, retrieval knobs, observability switches). Reads are live; a PATCH applies to the next run.
- **Run event bus + SSE** (`orchestrator/context.py`, `api/chat.py`) — `RunEventBus` is an in-memory per-run fan-out with history replay: `recorder.emit()` appends to history and pushes to subscriber queues; `GET /chat/stream/{run_id}` serves it via `sse-starlette`. Events are not persisted to a table — the durable trace is `run_steps` (see the data-flow section).
- **Observability** (`obs.py`, `orchestrator/recorder.py`) — every step start/finish writes a `run_steps` row, a structlog JSON event, Prometheus counters/histograms, an OTel span, and matching SSE events, all carrying the shared label set `{run_id, step_id, tier, kind, source, entity_id, entity_name, model, effort, tokens, duration_ms, status}`. LangSmith callbacks are built per run from live settings.

## Deployment

Topology from `docker-compose.yml` and `.env.example`:

```mermaid
graph TB
  host["Docker host"]

  subgraph net["compose network"]
    frontend["frontend<br>nginx :80 in-container<br>published FRONTEND_PORT (default 5173)"]
    backend["backend<br>uvicorn :8000<br>published BACKEND_PORT (default 8000)"]
    db[("db — postgres:16<br>no published port")]
    redis[("redis — redis:7-alpine<br>profile 'redis' only<br>published 127.0.0.1:6379")]
  end

  pgdata["named volume: pgdata<br>/var/lib/postgresql/data"]
  workspace["named volume: workspace<br>/workspace sandbox for the filesystem MCP server"]
  envfile[".env — API keys and config"]

  host -->|"FRONTEND_PORT:80"| frontend
  host -->|"BACKEND_PORT:8000"| backend
  frontend -->|"depends_on"| backend
  backend -->|"depends_on: service_healthy (pg_isready)"| db
  db --- pgdata
  backend --- workspace
  envfile -->|"ANTHROPIC_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY,<br>LANGSMITH_API_KEY, OTEL_EXPORTER_OTLP_ENDPOINT,<br>DATABASE_URL, REDIS_URL, FAKE_LLM_ENABLED"| backend
  envfile -->|"POSTGRES_USER / PASSWORD / DB"| db
  envfile -.->|"VITE_API_BASE_URL (build-time)"| frontend
  backend -.->|"REDIS_URL, only with --profile redis"| redis
```

- **Ports.** `frontend` publishes `${FRONTEND_PORT:-5173}:80`; `backend` publishes `${BACKEND_PORT:-8000}:8000`; `db` publishes nothing; `redis` (profile only) binds `127.0.0.1:6379:6379`.
- **Volumes.** `pgdata` persists Postgres; `workspace` is mounted at `/workspace` in the backend as the sandbox root for the seeded filesystem MCP server (`WORKSPACE_DIR`).
- **Env var flow for keys.** Provider, LangSmith, and Redis credentials travel exclusively `.env → compose environment → backend process env → app/config.py`. They are never written to Postgres, never returned by the API, never rendered in the UI. Key presence toggles provider availability at runtime.
- **Redis profile.** The default stack is three services. `docker compose --profile redis up` plus `REDIS_URL=redis://redis:6379/0` adds the optional cache backend; actually using it remains a Settings decision (`registry_cache_mode=redis`).
- **First boot.** The backend lifespan runs Alembic migrations and loads seeds (two stdio MCP servers — fetch and filesystem — one native tool, two native skills, the `research-concierge` sub agent), so a fresh `docker compose up` is fully self-provisioning.

## Data flow: one prompt, end to end

What happens to a single chat message in graph mode, what is persisted where, and what leaves the system:

```mermaid
graph TB
  msg["POST /api/v1/chat {message}"]
  runrow["INSERT conversations + runs row (status running)<br>Postgres: runs"]
  task["asyncio.create_task — run executes in-process"]
  plan["plan node: planner model call over registry summaries<br>optionally ranked top-K by retrieval"]
  planout["plan persisted on runs.plan<br>plan step row in run_steps"]
  resolve["resolve: ladder walks rungs per entry<br>route steps in run_steps"]
  dispatch["dispatch: parallel Send per entry<br>skill loops / compiled workers"]
  ckpt["LangGraph checkpoints<br>Postgres: checkpoint tables (AsyncPostgresSaver)"]
  hitl["HITL gate: interrupt() — run paused_hitl<br>resume replays from checkpoint"]
  toolcalls["tool_call steps in run_steps<br>result truncated to 4000 chars"]
  agg["aggregate: final answer + optional answer UI<br>runs.final_answer, runs.answer_ui, token totals"]
  sse["SSE events via in-memory RunEventBus<br>GET /chat/stream/{run_id} — not persisted"]

  llmapi["Provider APIs — prompts, tool schemas,<br>task text leave the system (HTTPS)"]
  mcpsrv["MCP servers — tool arguments leave the system<br>(stdio subprocess or streamable HTTP)"]
  lsmith["LangSmith — full traces when enabled"]
  otelc["OTel collector — spans when endpoint set"]
  embs["Embeddings API — record text when<br>embedding_model set; vectors stored on registry rows"]

  msg --> runrow --> task --> plan
  plan --> planout --> resolve --> dispatch
  dispatch --> ckpt
  dispatch --> hitl
  dispatch --> toolcalls
  dispatch --> agg
  plan -->|"model call"| llmapi
  dispatch -->|"skill loop model calls"| llmapi
  agg -->|"aggregator model call"| llmapi
  toolcalls --> mcpsrv
  task -.-> lsmith
  task -.-> otelc
  plan -.-> embs
  plan --> sse
  dispatch --> sse
  agg --> sse
```

Persistence inventory:

| Data | Where | Written by |
|---|---|---|
| Conversations, runs (status, plan, final answer, answer UI, token totals) | `conversations`, `runs` tables | `orchestrator/runner.py`, `graph_mode.py` |
| Step-level trace (plan/route/skill/hitl/tool_call/aggregate, parent links, tokens, errors) | `run_steps` table | `orchestrator/recorder.py` |
| Graph execution state for HITL pause/resume and worker replay | LangGraph checkpoint tables, created by `AsyncPostgresSaver.setup()` | `db.py` checkpointer, keyed by `thread_id = run_id` (workers: `run_id:entry_id`) |
| Registry definitions and settings | `tools`, `skills`, `sub_agents`, `mcp_servers`, `app_settings` | API routers, MCP ingest, native scan, seed loader |
| Embedding vectors + content hashes | `embedding` / `embedding_hash` JSON columns on the three registry tables (plain columns, not pgvector) | `retrieval.py` backfill via the embeddings port |
| Live SSE event stream | in-memory `RunEventBus` only — replayable while the process lives, gone on restart; `run_steps` is the durable record | `orchestrator/context.py` |

What leaves the system: model prompts (including registry summaries, skill instructions, and task text) to whichever provider each model ref names; tool arguments and results to/from MCP servers; traces to LangSmith and spans to an OTLP endpoint only when those switches are on. Nothing else calls out.

## Key design decisions

Each decision has a full ADR; one-paragraph summaries here.

- **[ADR 0001 — No broker, single process](../adr/0001-no-broker-single-process.md).** Runs are `asyncio.Task`s inside the one FastAPI process; Postgres is the only mandatory stateful service. HITL pause/resume rides the LangGraph checkpointer rather than a queue, and cancellation is cooperative task cancellation — eliminating Celery/Redis/broker operational surface at POC scale.
- **[ADR 0002 — ModelProvider port](../adr/0002-model-provider-port.md).** All model access goes through `get_model("provider:model")` against a decorator-populated adapter registry in `backend/app/llm/`; no provider SDK import exists outside that package, including for Anthropic. A custom gateway adapter drops in with zero consumer changes, and every adapter passes a shared contract test suite.
- **[ADR 0003 — Middleware precedence](../adr/0003-middleware-precedence.md).** Out-of-box LangChain middleware first (TodoList, Summarization, ModelCallLimit), composition hooks second, custom middleware only where nothing OOB fits — which is exactly the three registry projections. `build_middleware_stack(context)` is the single sanctioned composition path, and skill-loop isolation (bound tools only) is enforced by stack construction, not convention.
- **[ADR 0004 — Registry cache with bypass default](../adr/0004-registry-cache-bypass-default.md).** Every run-path registry read goes through one `RegistryCache` facade whose backend flips live between `bypass`, `memory`, and `redis`. `bypass` ships as the default and the rollback lever: correctness first, caching as an opt-in optimization, with event invalidation (no TTLs) keeping visibility at "next model call".
- **[ADR 0005 — Hybrid retrieval, BM25 + RRF](../adr/0005-hybrid-retrieval-bm25-rrf.md).** Catalog ranking runs in-process over the cache snapshot: dependency-free Okapi BM25 plus optional embedding cosine, fused by reciprocal-rank fusion. No per-call DB query, graceful degradation to lexical-only when no embedding model is configured, and pinned ids so in-flight entities never vanish from a ranked catalog.
- **[ADR 0006 — JSONB embeddings before pgvector](../adr/0006-jsonb-embeddings-before-pgvector.md).** Vectors are stored as plain JSON columns (`embedding`, `embedding_hash`) on the registry tables and scored in Python. At hundreds of records, an ANN index buys nothing; deferring pgvector keeps the schema portable and the Postgres image stock until scale demands otherwise.
- **[ADR 0007 — OpenAI Responses API routing](../adr/0007-openai-responses-api-routing.md).** OpenAI reasoning models reject function tools combined with `reasoning_effort` on chat completions, so the OpenAI adapter routes any effort-bearing run through the Responses API (`use_responses_api=True`, `output_version="responses/v1"`) while returning the same `BaseChatModel` — the quirk is contained entirely inside the adapter.
- **[ADR 0008 — LISTEN/NOTIFY for cross-replica invalidation](../adr/0008-listen-notify-cross-replica.md).** Cache invalidations broadcast over Postgres `pg_notify` on the `registry_cache_inv` channel with an origin id to skip self-notifications. Dormant and harmless on a single node, it makes the memory/redis cache modes multi-replica-safe without adding a message bus.
- **[ADR 0009 — Skills as markdown](../adr/0009-skills-as-markdown.md).** One document format — YAML frontmatter plus markdown instructions — serves both native `.skill.md` files scanned at startup and UI-authored custom skills, parsed by `backend/app/skilldoc.py`. Skills stay human-reviewable, diffable prompts with declared tool bindings rather than opaque config.
- **[ADR 0010 — Two orchestrator modes](../adr/0010-two-orchestrator-modes.md).** The deterministic graph pipeline and the emergent agentic loop share the registries, ladder executor, recorder, and label set, and switch per run via one setting — a live A/B harness for the central open question of how much orchestration to hand the model.

## Related documents

- [Data model](./data-model.md) — tables, relationships, migration strategy
- [Components](./components.md) — per-component deep dives
- [Runtime flows](./runtime-flows.md) — sequence diagrams for chat, MCP plug-in, HITL
- [State machines](./state-machines.md) — run and MCP server lifecycles
- [Resolution ladder](./resolution-ladder.md) — rung-by-rung semantics and fallbacks
