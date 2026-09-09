# Code Tour

A guided walk through the codebase. Spec section references (`§n`) point into `spec.md`.

## Backend — `backend/app/`

### Top-level modules

- **`main.py`** — FastAPI application factory (`create_app`) and startup lifecycle: run Alembic migrations, load seeds, create checkpointer tables, warm the registry cache, start the MCP manager and the embeddings backfill as non-blocking tasks. Also mounts `/health` and Prometheus `/metrics`.
- **`config.py`** — `AppConfig` (pydantic-settings): env-only configuration and secrets (§13). Treats blank env strings as "unset" so compose's `${VAR:-}` passthrough never produces a falsely-configured provider.
- **`db.py`** — async SQLAlchemy engine/session factory plus the shared LangGraph Postgres checkpointer (`get_checkpointer` / `close_checkpointer`) and `reset_db_state` for tests.
- **`obs.py`** — observability (§10): structlog JSON logging, Prometheus counters/histograms, OTel spans, per-run LangSmith tracer construction — all carrying the shared label set (`run_id, step_id, tier, kind, source, entity_id, entity_name, model, effort, …`).
- **`overlap.py`** — the LLM-as-judge overlap guard (§4) backing `POST /skills/check-overlap` and `/sub-agents/check-overlap`: builds candidate lists (skills vs skills+tools, sub agents vs sub agents+skills), runs `overlap_judge.md` through the provider port with structured `OverlapVerdict` output, and fails open on any judge trouble.
- **`registry_cache.py`** — the M7 cache layer (§7.3): singleton `RegistryCache` facade over every registry/settings read in the run path, with live-swappable backends (`bypass` / `memory` / `redis`), per-registry generation counters, reload-on-dirty, `pg_notify` cross-replica invalidation with origin filtering, and the `invalidate` / `refresh` / `status` operations behind `/api/v1/cache/*`.
- **`retrieval.py`** — progressive-disclosure retrieval (§7.4): BM25 + embedding-cosine scoring fused by RRF over the cache snapshot, threshold gate, pinned ids, truncation footers, write-path embedding maintenance and the startup `backfill_embeddings` task.
- **`settings_store.py`** — `app_settings` defaults, live reads, and validated writes (§3.7); a PATCH applies to the next run with no restart and invalidates the settings cache.
- **`skilldoc.py`** — skill document parsing (§3.3): frontmatter + markdown body, `{tool:...}` mention extraction/validation, and the `.skill.md` startup scan — one format shared by native files and UI-authored custom skills.

### Packages

- **`api/`** — one router per resource, aggregated in `api/__init__.py` under `/api/v1`: `mcp_servers.py`, `tools.py`, `skills.py`, `sub_agents.py` (registries, §4), `chat.py` (conversations, `POST /chat`, SSE stream, HITL resolve), `runs.py` (list/detail/cancel/retry/delete/purge), `settings.py` (+ HITL pending queue), `seed.py` (idempotent reload), `cache.py` (cache status/refresh, §8.7), `fake_llm.py` (`/_fake` script control, 404-invisible unless `FAKE_LLM_ENABLED`), and `deps.py` (shared list filters + static-record write rules).
- **`models/`** — SQLAlchemy ORM, one module per tier: `base.py` (`Base`, `RegistryRecord` with the common id/name/source/status/soft-delete columns), `mcp_server.py`, `tool.py`, `skill.py`, `sub_agent.py`, `run.py` (`Conversation`, `Run`, `RunStep`), `setting.py`.
- **`schemas/`** — Pydantic v2 API schemas, deliberately separate from ORM models (§13): `common.py`, `mcp_server.py`, `tool.py`, `skill.py`, `sub_agent.py`.
- **`llm/`** — the provider layer (§2.1), **the only module tree allowed to import provider SDKs**. `port.py` (the `ModelProvider` protocol, `ModelParams`, `ModelInfo`), `registry.py` (`@model_provider` registration, the single entry points `get_model()` / `get_embeddings()`, save-time validation), `adapters.py` (anthropic / google_genai / openai wrappers with effort→provider-knob mapping), `fake.py` (the scriptable `fake:scripted` provider tests and keyless demos use), `content.py` (provider-neutral extraction of text from reasoning-block message content).
- **`mcp/`** — `manager.py`: the singleton MCP connection manager (§5). One asyncio task per active server holding the client session; `tools/list` ingest into the tools registry, `listChanged` reconciliation, health ping loop, reconnect/refresh endpoints' backing logic, and LangChain tool objects for the factory.
- **`factory/`** — the worker factory (§6). `dag.py` validates workflow DAG structure at save (single START, path to END, no cycles, unique nodes, ≤1 error edge per node). `worker.py` compiles a sub-agent snapshot into a LangGraph `CompiledStateGraph`: explicit StateGraph shell (routing, error edges, `Send` fan-out, joins, `interrupt()`), `create_agent` tool loops at skill nodes, prompt assembly in spec order (`assemble_skill_prompt`), model resolution order skill→agent→defaults (`resolve_node_model`), and the condition router LLM call.
- **`native/`** — code-defined capabilities (§5b): `provider.py` (the `@native_tool` / `@native_sub_agent` decorator scan, schema derivation, guardrails — no HITL inside subgraphs, no registry-sub-agent wrapping), `tools.py` (the `summarize-and-structure` subgraph-as-tool and the `render_chart` validator tool), `skills/*.skill.md` (the seeded `web-research` and `file-ops` skill documents).
- **`orchestrator/`** — §7 end to end. `runner.py` (run lifecycle: asyncio task per run, cooperative cancel, HITL pause/resume on the checkpointer); `graph_mode.py` (plan→resolve→dispatch→aggregate StateGraph); `agentic_mode.py` (single `create_agent` concierge with the full middleware stack); `planner.py` (progressive-disclosure prompt, structured plan output, validate→repair-once→fail); `ladder.py` (deterministic capability resolution: direct → native → custom → ephemeral worker, plus execution of each rung); `middleware.py` (the three registry-projection middlewares and `build_middleware_stack`); `answer_ui.py` (model-generated component tree → deterministic A2UI v0.9 translation); `recorder.py` (run/step recording + every observability emission); `context.py` (the per-run contextvar `RunContext` carried through every coroutine).
- **`prompts/`** — every LLM prompt as a versioned `.md` file plus the `load_prompt()` loader. Full catalog in [prompts.md](./prompts.md).
- **`seed/`** — `loader.py`: idempotent static seed (§9), upserting on `(source='static', name)` so ids stay stable across reloads.

Also: `backend/alembic/versions/` — one migration per schema change (initial schema, `runs.answer_ui`, registry embeddings, `skills.max_tool_iterations`).

## Frontend — `frontend/src/`

- **`App.tsx`** — HashRouter shell: left nav (Chat, MCP Servers, Tools, Skills, Sub Agents, Runs, Settings), TanStack Query client, orchestrator-mode indicator.
- **`main.tsx` / `index.css`** — entry point and the Tailwind 4 theme tokens (mission-control default palette).
- **`theme.ts`** — client-side theme switching (§8.7): four palettes (`default`, `anthropic`, `openai`, `google`) applied via a `data-theme` attribute and persisted in localStorage.
- **`api/client.ts`** — thin fetch wrapper over `/api/v1` (`ApiError` with detail extraction) plus the SSE subscription (`EventSource` on `/chat/stream/{run_id}`).
- **`api/hooks.ts`** — TanStack Query hooks per resource: `useServers`, `useTools`, `useSkills`, `useSubAgents`, `useRuns`/`useRun`, `useConversations`/`useConversation`, `useSettings`/`usePatchSettings`, `useProviders`, `useHitlPending`, `useCacheStatus`/`useRefreshCache`, `useInvalidate`.
- **`api/types.ts`** — the TypeScript mirror of the backend schemas and SSE event payloads.
- **`pages/`** — one page per nav entry: `ChatPage.tsx` (streaming chat, plan/todo cards, activity ticker, HITL cards incl. form gates, Stop/queue, answer panel), `McpServersPage.tsx` (register form with test-connection, masked env/headers, reconnect/refresh), `ToolsPage.tsx` (skill badges, expose toggle, schema drawer), `SkillsPage.tsx` (skill-document editor with preview and `{tool:}` autocomplete, overlap dialog), `SubAgentsPage.tsx` (workflow builder with starter templates and react-flow preview, validation errors inline), `RunsPage.tsx` (trace timeline grouped by sub agent, cancel/retry/delete), `SettingsPage.tsx` (the command center: models/params, orchestrator toggles, cache + retrieval controls, MCP ops, observability, HITL queue, themes, data ops).
- **`components/`** — `RegistryTable.tsx` (the consistent table pattern of §8), `ui.tsx` (badges, pills, chips, drawer, toggle, masked values, and the rest of the primitive kit), `AnswerPanel.tsx` (markdown answer + collapsed "structured summary"), `AnswerUiView.tsx` (official `@a2ui/react` renderer), `ChartSvg.tsx` (themed pure-SVG bar/line/pie), `Markdown.tsx` (safe renderer, no raw HTML), `OverlapDialog.tsx` (save-anyway/cancel judge dialog), `CacheControls.tsx` (per-registry refresh button + status line).
- **`test/`** — vitest suites (`ui.test.tsx`, `answer-ui.test.tsx`) with `setup.ts` for jest-dom.

## Where would I change X?

| Task | Start here |
|---|---|
| Add a model provider adapter | `backend/app/llm/adapters.py` + `backend/app/llm/registry.py` (register), `backend/app/config.py` (key env var); must pass `backend/tests/test_llm_contract.py` — checklist in [testing.md](./testing.md#a-new-provider-adapter) |
| Add an `app_settings` key | `backend/app/settings_store.py` (default + validation), surface it in `frontend/src/pages/SettingsPage.tsx` + `frontend/src/api/types.ts`; spec §3.7 lists the canonical keys |
| Add an SSE event type | Emit in `backend/app/orchestrator/recorder.py` (or `runner.py`/`chat.py` for lifecycle events), extend the contract in `backend/app/api/chat.py`, consume in `frontend/src/api/client.ts` + `frontend/src/pages/ChatPage.tsx`; contract test in `backend/tests/test_orchestrator.py::TestSseAndMetrics` |
| Add a workflow DAG node type | `backend/app/factory/dag.py` (validation) + `backend/app/factory/worker.py` (compilation), schema in `backend/app/schemas/sub_agent.py`, builder UI in `frontend/src/pages/SubAgentsPage.tsx`; tests in `backend/tests/test_factory.py` — spec §3.5 change required first |
| Add a native tool | `backend/app/native/tools.py` with `@native_tool` (guardrails in `provider.py` apply); tests in `backend/tests/test_native_provider.py` |
| Add a native skill | Drop a `.skill.md` in `backend/app/native/skills/` (frontmatter + body, `{tool:...}` mentions must resolve); parsing rules in `backend/app/skilldoc.py` |
| Change a prompt | Edit the file in `backend/app/prompts/` — see [prompts.md](./prompts.md); never inline a prompt string |
| Add a DB column/table | SQLAlchemy model in `backend/app/models/`, new migration in `backend/alembic/versions/` (one per schema change), Pydantic schema in `backend/app/schemas/` |
| Add a registry write path | The endpoint/module doing the write **plus** `get_cache().invalidate(...)` before returning — see [testing.md](./testing.md#a-new-registry-write-path) |
| Add a UI page | `frontend/src/pages/`, route + nav entry in `frontend/src/App.tsx`, hooks in `frontend/src/api/hooks.ts` |
| Change run/step labels or metrics | `backend/app/obs.py` + `backend/app/orchestrator/recorder.py` — the §10 label set is mandatory on every span/log/metric |
| Tune the resolution ladder or fallback | `backend/app/orchestrator/ladder.py` (rungs) + `backend/app/orchestrator/middleware.py` (full-catalog mode); per-rung tests in `backend/tests/test_orchestrator.py::TestResolutionLadder` |
