# Contributing

This repo is developed **spec-first**. `spec.md` at the repo root is the complete, binding specification; `CLAUDE.md` encodes the workflow rules that every change — human or agent — must follow. Read both before touching code.

## The spec-driven workflow

1. **`spec.md` is the single source of truth.** Implement it as written. If the spec is ambiguous or two sections conflict, stop and ask — do not invent behavior. If you believe the spec is wrong, say so and propose a spec change *before* coding it (spec changes land as their own `docs(spec):` commits, e.g. `38f9fd6 docs(spec): registry cache layer (§7.3) and progressive-disclosure retrieval (§7.4)`).
2. **Milestone discipline** (spec §12). Work proceeds strictly milestone by milestone: M1 (registries + seed) → M2 (MCP manager) → M3 (worker factory) → M4 (orchestrator + SSE + observability) → M5 (admin UI) → M6 (test/compose polish) → M7 (registry cache + retrieval). A milestone does not start until the previous one's tests are green.
3. **Per-milestone loop** (from `CLAUDE.md`): restate the milestone's scope by listing the spec sections it implements → write tests first → implement until green → run the executable steps of the Acceptance Demo Script (spec §14) → update `README.md` (milestone status table + layout changes) → one conventional commit per coherent change → checkpoint summary, then continue.
4. **Verification means executed proof.** Show test output; show `curl` output for API milestones; show screenshots for UI work. Never declare something done without it.

## Commits

Conventional commits, enforced by convention and review. Real examples from `git log`:

```
feat(cache): LISTEN/NOTIFY cross-replica invalidation + optional Redis provisioning in quick-setup
feat(m8): markdown answers + summary toggle, HITL form gates, charts, per-skill loop budgets
feat(llm): current Gemini flash models in the google_genai adapter list
fix(llm): route OpenAI reasoning-effort runs through the Responses API
fix(ui): never leave a HITL card armed after its gate was consumed
fix(scripts): bash 3.2 portability for macOS
docs(acceptance): stage 21 — M8 features on the Claude × Gemini combo
docs(spec): registry cache layer (§7.3) and progressive-disclosure retrieval (§7.4)
docs(readme): M7 milestone — registry cache + retrieval
```

Rules of thumb:

- `type(scope): summary` — types in use: `feat`, `fix`, `docs`, plus the standard set. Scopes in use: `llm`, `cache`, `ui`, `chat`, `retrieval`, `scripts`, `spec`, `acceptance`, `readme`, and milestone tags like `m8`.
- One coherent change per commit. Evidence commits (`docs(acceptance): …`) are separate from the feature commits they prove.
- An Alembic migration accompanies every schema change, in the same commit as the model change (see `backend/alembic/versions/`).

## Branches

- `main` is the integration branch. Feature work happens on descriptive branches and lands via pull request (e.g. branch `claude/concierge-agent-poc-9bfkd0`, merged as `dd6bfc9 Merge pull request #2 …`).
- Do not commit directly to `main`.

## Hard constraints that reviews enforce

These are the non-negotiables from `CLAUDE.md` / spec. A PR that violates any of them gets rejected regardless of how well it works:

1. **Provider layer is never bypassed** (spec §2.1). All model access goes through `get_model("provider:model")` in `backend/app/llm/registry.py` — for *every* provider, including Anthropic. No provider SDK or LangChain provider-package import outside `backend/app/llm/`. Structured outputs via LangChain abstractions only; token accounting via `usage_metadata` only. Every adapter must pass the shared contract suite (`backend/tests/test_llm_contract.py`).
2. **Middleware precedence** (spec §7.0). Out-of-box LangChain middleware first, configured via options; compose/subclass hooks second; custom middleware only when nothing OOB fits. The only sanctioned custom middlewares are the three registry projections in `backend/app/orchestrator/middleware.py`. All stacks are built through `build_middleware_stack(context)`; a skill loop gets scoped `ToolsRegistry` only — never the Skills or SubAgents registry middlewares.
3. **No broker, no queue, no Celery.** Runs are asyncio tasks in the single FastAPI process (`backend/app/orchestrator/runner.py`); SSE is plain HTTP; HITL rides the LangGraph Postgres checkpointer. Redis exists solely as the optional registry-cache backend behind the compose `redis` profile — nothing else may depend on it.
4. **Prompts are files.** Every LLM prompt lives in `backend/app/prompts/*.md` and loads via `load_prompt()`. No inline prompt strings, anywhere. See [prompts.md](./prompts.md).
5. **Env-only keys.** Provider and LangSmith API keys live in environment variables only — never in the database, never in the UI, never logged (`backend/app/config.py`).
6. **Registry `id`s are immutable; static records are protected.** `source='static'` records reject definition writes with 403; only `status` and `direct_exposure` are togglable (spec §4, enforced in `backend/app/api/deps.py` and covered by `backend/tests/test_registry_api.py`).
7. **Every span/log/metric carries the spec §10 label set** (`run_id, step_id, tier, kind, source, entity_id, entity_name, model, effort, input_tokens, output_tokens, duration_ms, status` — see `backend/app/obs.py`).
8. **Every registry write path invalidates the cache before returning** (spec §7.3). If you add a write path, call `get_cache().invalidate(<registry>)` — see [testing.md](./testing.md#adding-a-new-registry-write-path).

## Definition of done

All milestones complete, and on a fresh checkout:

```bash
cd backend && pytest                      # backend suite green (needs test Postgres, see local-development.md)
cd backend && ruff check . && mypy app    # lint + strict typing clean
cd frontend && npm run lint && npm run test
docker compose up                         # then: spec §14 acceptance script passes top to bottom
```

The eleven-step Acceptance Demo Script (spec §14) on a fresh `docker compose up` is the final gate — not a formality. The full manual run that proved it lives in `docs/acceptance/`.

## PR expectations

Attach **executed proof**, matched to what the change touches:

- **Any backend change**: the relevant `pytest` output (at minimum the affected module, ideally the full suite) plus `ruff check . && mypy app`.
- **API-facing change**: `curl` transcripts of the new/changed endpoints — this is the CLAUDE.md standard for API milestones.
- **UI-facing change**: screenshots following the `docs/acceptance/` practice — numbered stage folders (`docs/acceptance/21-m8-features/`, `docs/acceptance/22-hitl-stale-card-fix/`, …) containing before/after or walk-through screenshots, indexed in `docs/acceptance/README.md` with the spec sections each stage proves. Evidence lands as its own `docs(acceptance):` commit (see `becb1db`, `f686735`'s companion stage `22-hitl-stale-card-fix`).
- **Provider/adapter change**: contract-suite output (`pytest tests/test_llm_contract.py`), and where a real key is available, a note of the live smoke result.
- **Anything touching orchestration**: confirm the orchestrator suite passes in **both** cache modes (it is parametrized over `bypass` and `memory` automatically — just run `pytest tests/test_orchestrator.py` and say so).

If the change alters behavior described in spec §14, state which acceptance steps you re-ran.
