# CLAUDE.md — Concierge Agent POC

## Source of truth
`spec.md` at repo root is the complete specification. Implement it as written. If the spec is ambiguous or two sections conflict, stop and ask — do not invent behavior. If you believe the spec is wrong, say so and propose the change before coding it.

## Workflow
- Work strictly milestone by milestone (spec §12): M1 → M2 → M3 → M4 → M5 → M6. Do not start a milestone until the previous one's tests are green.
- Per milestone: (1) restate the milestone's scope by listing the spec sections it implements, (2) write tests first for the behavior in those sections, (3) implement until green, (4) run the relevant Acceptance Demo Script steps (spec §14) that are executable at this stage, (5) update README.md (milestone status table, plus any getting-started or layout details that changed), (6) single conventional commit per coherent change, (7) write a checkpoint summary (what was built, what was deferred to which milestone, ambiguities hit) and proceed directly to the next milestone. Do not wait for review between milestones. Pause and ask ONLY when the spec is ambiguous or self-contradictory — resolve, then continue.
- Verification means executed proof: show test output, show curl output for API milestones. Never declare a milestone done without it.

## Hard constraints
- **Provider layer is non-negotiable basic design (spec §2.1)**: the `ModelProvider` port + registry in `backend/app/llm/` is used for every provider including Anthropic — never bypassed, never special-cased. All model access via `get_model("provider:model")`. No provider SDK or LangChain provider package imports outside `app/llm/`. Structured outputs via LangChain abstractions only; token usage via `usage_metadata`. Every adapter must pass the shared adapter contract test suite.
- **Middleware precedence (spec §7.0)**: out-of-box LangChain middleware first, configured via options; compose/subclass hooks second; custom middleware only when nothing OOB fits — the only sanctioned custom middlewares are the three registry projections. All stacks built through `build_middleware_stack(context)`; never attach registry middlewares other than scoped ToolsRegistry to a skill loop.
- No message broker, task queue, Redis, or Celery — asyncio in one FastAPI process (spec §2). Do not add infrastructure beyond the three compose services.
- Provider API keys: env only. Never in DB, never in UI, never logged.
- All LLM prompts live in `backend/app/prompts/` as files. No inline prompt strings.
- Registry `id`s immutable. Static records: definition writes rejected; only `status`/`direct_exposure` togglable (spec §4).
- Every span/log/metric carries the label set from spec §10.

## Conventions (spec §13)
- Python 3.12: ruff (lint+format), mypy strict on `app/`, pytest, async SQLAlchemy, Pydantic v2 schemas separate from ORM models, Alembic migration per schema change.
- TypeScript: eslint + prettier, strict tsconfig, TanStack Query, no Redux.
- Conventional commits.

## Testing models
- Unit/integration suites run on the fake provider (`FAKE_LLM_ENABLED=1`) — deterministic, key-free (spec §11).
- Live acceptance / milestone proofs ALWAYS use a real LLM; prefer `openrouter:qwen/qwen3.8-max` as `default_model` (all roles) unless a stage specifically tests another provider.

## Commands
- Backend tests: `cd backend && pytest`
- Lint: `cd backend && ruff check . && mypy app && python -m app.doclint`
- Seed documents (`.skill.md` / `.agent.md`) only: `cd backend && python -m app.doclint` — same checks the seed applies, offline; also a Docker build gate, so a malformed document fails the build instead of surfacing at boot.
- Frontend: `cd frontend && npm run lint && npm run test`
- Full stack: `docker compose up`

## Definition of done
All six milestones complete, all test suites green, and the ten-step Acceptance Demo Script (spec §14) passes top to bottom on a fresh `docker compose up`.
