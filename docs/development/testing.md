# Testing

## Philosophy

- **No keys, ever.** All LLM behavior in tests is injected through the provider port via the scriptable fake provider (`fake:scripted`, `backend/app/llm/fake.py`). `backend/tests/conftest.py` sets `FAKE_LLM_ENABLED=1` and *removes* any real provider keys from the environment before importing the app, so tests exercise the identical `get_model()` resolution path without ever touching a provider SDK (spec §11). Script responses with `fake_llm.push_ai(content, tool_calls)` / `push_error(exc)`; assert tool bindings with `seen_tools()`.
- **Contract tests gate every adapter.** `backend/tests/test_llm_contract.py` is the shared suite every registered `ModelProvider` must pass — port shape, unconfigured refusal, `BaseChatModel` return, `ModelParams`/effort mapping, tool-calling round-trip, structured output, `usage_metadata` — parametrized over all registered providers. This suite is what makes a future custom gateway adapter safe to drop in (spec §2.1).
- **Dual-cache-mode no-degradation gate.** The entire orchestrator suite (`test_orchestrator.py`) runs twice via an autouse fixture parametrized over `registry_cache_mode` in `["bypass", "memory"]` — the M7 cache layer is only correct if every orchestrator behavior is bit-identical in both modes. The cache contract suite (`test_registry_cache.py::TestCacheContract`) does the same for the cache's own typed reads.
- **Test Postgres required.** The suite owns a real database (default `postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test`, override with `TEST_DATABASE_URL`); it drops/creates the schema per session and truncates all tables between tests. See [local-development.md](./local-development.md#backend) for setup.
- **Redis tests are env-gated.** `test_registry_cache.py::TestRedisBackend` (and the redis leg of mode-select validation) skip without `REDIS_URL` — excluded from the default gate by design (spec §7.3).

## Running

```bash
cd backend
uv run pytest                              # everything
uv run pytest tests/test_factory.py        # one module
uv run pytest tests/test_orchestrator.py -k Ladder   # one class/keyword
uv run pytest -k "bypass"                  # only the bypass-mode parametrization
cd ../frontend
npm run test                               # vitest (jsdom), src/test/*.test.tsx
```

## Suite map (`backend/tests/`)

| Module | Covers |
|---|---|
| `conftest.py` | Fixtures: test DB (drop/create + truncate), fake-LLM script reset, registry-cache singleton reset per test, `client` (httpx ASGI) and `seeded_client` (POST `/seed/reload` first). |
| `factory_helpers.py` | Shared builders that write registry rows directly — and invalidate the cache afterward, matching the contract every real write path honors (spec §7.3). |
| `stub_mcp_server.py` | FastMCP stdio server with a mutable toolset (`echo`/`add` → `mutate_toolset` → `extra_tool` + `tools/list_changed`; `die` hard-exits for health tests). |
| `test_config.py` | `AppConfig` env parsing — compose passes `${VAR:-}`, so blank strings must mean "unset", never a falsely-configured provider. |
| `test_llm_contract.py` | The shared adapter contract suite (spec §2.1): port shape, configured/unconfigured behavior, bad-ref rejection, tool calling, structured output, usage metadata, `ModelParams` effort→knob mapping, save-time model validation — parametrized over every registered provider. |
| `test_skilldoc.py` | Skill document parsing (spec §3.3): frontmatter + body, `{tool:...}` mention extraction/validation (untagged mention rejected), `.skill.md` directory scan. |
| `test_seed.py` | Seed contents (spec §9) and idempotency: 2 MCP servers, native skills from `.skill.md`, native tools, `research-concierge`. |
| `test_registry_api.py` | Registry API contract (spec §4): CRUD + filters per registry, 403 static writes, 409 dependents, strict `skill_id` references, DAG save validation, settings validation, providers panel, `/_fake` script control. |
| `test_factory.py` | Worker factory (spec §6): DAG→StateGraph compile for sequential/branch/parallel + reachable joins/error edges/HITL, ephemeral multi-skill build, persona merge order, tool isolation, `max_tool_iterations`, compile cache keyed by `updated_at`, compile-at-save, Postgres checkpointer, native sub agents. |
| `test_mcp_manager.py` | MCP manager (spec §5) against the stub server: stdio + http connect, tool ingest, `listChanged` reconcile, error status, health loop, invocation, startup reload, stdio env passing. |
| `test_native_provider.py` | Native provider (spec §5b): registration scan, schema derivation, guardrail rejections (HITL-in-subgraph, sub-agent wrapping), mixed mcp+native skill invocation, subgraph-as-tool with nested trace/token rollup, structured-summary repair retry. |
| `test_overlap.py` | Overlap guard (spec §4): ≥70% flags with match + reasoning, `exclude_id` on updates, fail-open on judge trouble, skills judged against skills+tools, sub agents against sub agents+skills. |
| `test_orchestrator.py` | The big one (spec §7, §11), **parametrized over bypass+memory cache modes**: chat→run→HITL happy path, resolution ladder (one test per rung + precedence), plan validate/repair/fail, full-catalog fallback + strict tool isolation even in fallback, cancel/retry, agentic mode over the same fixtures, SSE event contract, answer UI, `/metrics`, run housekeeping, reasoning-block content, parallel HITL, live middleware sync, `spin_worker` strict ids, ephemeral-worker exposure gate, tool-failure containment, chat presentation contracts, lineage/callsigns. |
| `test_registry_cache.py` | Cache layer (spec §7.3): contract suite identical over `bypass` and `memory` (typed reads, invalidation-after-write ordering, refresh/status endpoints), mode-flip validation, **env-gated Redis backend tests** (`REDIS_URL`), and cross-replica `pg_notify` sync (peer invalidation, origin filtering). |
| `test_retrieval.py` | Retrieval (spec §7.4): ranker units (BM25, vector via fake embeddings, RRF fusion), threshold gate + pinned ids + footer on the planner catalog, write-path/backfill embeddings pipeline. |
| `test_m8_features.py` | M8 features: per-skill `max_tool_iterations` override (§3.3), HITL form gates incl. malformed-spec degradation (§3.5), chart split-out and themed rendering path (§7.1), `render_chart` native tool validation (§5b). |

Frontend (`frontend/src/test/`, vitest + testing-library, jsdom): `ui.test.tsx` (badge/pill/table primitives — the consistent table pattern of spec §8) and `answer-ui.test.tsx` (A2UI renderer over valid/invalid payloads).

## How to add …

### A new provider adapter

1. Implement the `ModelProvider` port (`backend/app/llm/port.py`): `provider_id`, `is_configured()`, `list_models()`, `get_chat_model()`, plus `supports_embeddings()`/`get_embeddings()` (raise if unsupported).
2. Register it with the `@model_provider` decorator and wire it into `register_builtin_providers()` (`backend/app/llm/registry.py`, adapters live in `backend/app/llm/adapters.py`).
3. Map the normalized `ModelParams` — especially `effort` — onto the provider's own knob; declare per-model supported params in `ModelInfo` so unsupported combinations 422 at save.
4. Gate configuration on an env var only (`backend/app/config.py`); never accept a key via DB or UI.
5. Import nothing provider-specific outside `backend/app/llm/` — consumers only ever see `get_model("yourprovider:model")`.
6. **Finish line: the shared contract suite must pass unchanged** — `uv run pytest tests/test_llm_contract.py`. It auto-discovers registered providers; if it needed edits for your adapter, the adapter is wrong, not the suite.

### A new registry write path

Any code path that creates, updates, deletes, toggles, ingests, or seeds registry rows **must call `get_cache().invalidate(<registry>)` before returning** (spec §7.3: "Invalidation is event-driven and exhaustive … TTLs are forbidden; a cache entry is either current or explicitly invalidated"). Follow the existing call sites: `backend/app/api/skills.py`, `backend/app/api/tools.py`, `backend/app/api/sub_agents.py`, `backend/app/api/mcp_servers.py`, `backend/app/api/seed.py`, `backend/app/mcp/manager.py`, `backend/app/settings_store.py`. Then prove it: add a case to `tests/test_registry_cache.py` asserting the write is visible through the cache in **memory** mode without a manual refresh (the contract-suite pattern), and note that the orchestrator suite will exercise your path in both modes automatically. Test helpers that write rows directly must do the same (`tests/factory_helpers.py` shows how).

### A new middleware

Follow the precedence rules of spec §7.0 strictly, in this order:

1. **Out-of-box first**: can an existing LangChain middleware be configured to do it (Summarization, TodoList, call limits)? Use it via options.
2. **Compose/subclass second**: can you subclass or compose an existing hook? Do that.
3. **Custom last**: only when nothing OOB fits. The only sanctioned custom middlewares are the three registry projections in `backend/app/orchestrator/middleware.py` — a new custom middleware needs a spec change first (see [contributing.md](./contributing.md)).

Whatever you add, it enters agents exclusively through `build_middleware_stack(context)` — never attached ad hoc to a `create_agent` call — and must respect the stack rules: skill loops get scoped `ToolsRegistry` only (isolation is structural, not advisory); the agentic orchestrator gets all three registry middlewares + TodoList + Summarization + limits; the graph-mode shell gets none. Registry middlewares must stay stateless projections (fresh read per model call, no shared state). Cover it in `tests/test_orchestrator.py` so it runs under both cache modes, and add isolation assertions via `fake_llm.seen_tools()` if it touches tool exposure.
