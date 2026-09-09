# Changelog

All notable changes to the Concierge Agent POC. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); entries are grouped
by milestone (spec §12) instead of semver releases, newest first. The project
was built spec-first, milestone by milestone, on a single feature branch:
milestones M1–M8 landed via [PR #2] (merged 2026-08-07, superseding the
earlier [PR #1] merge of the M1–M6 line); the HITL card fix landed via
[PR #3].

## M12 — Declarative .agent.md sub-agents — 2026-08-09

### Added

- **`.agent.md` (spec §3.4)**: a third authoring path onto the same
  sub_agents registry row — YAML frontmatter (name, description, persona,
  model/model_params, direct_exposure, §3.5 workflow) + a documentation
  body. Skill nodes reference skills by NAME; the seed pass resolves them
  to registry uuids, validates with the same structural + factory-compile
  checks the API applies at save, and upserts keyed on (name, static) with
  file provenance in native_ref.
- **Lifecycle**: invalid files land as status='error' (UI-visible, logged,
  never crash boot); a fixed file flips error→active; a removed file marks
  its row inactive (never deletes); user status/direct_exposure toggles
  survive reseeds while definition fields always follow the file.
- **Seeded example**: workspace-reporter.agent.md — audit(workspace-auditor)
  → form gate (report format + free-text addendum) → write(file-ops), with
  an empty-workspace branch; ships direct_exposure=true, so every §7.5
  surface works on a file-defined agent out of the box.
- 9 tests: parser rejections, seed resolution + static rules + form-gate
  direct invocation, and the full reseed lifecycle (toggle survival,
  error→active recovery, file-removal deactivation, malformed-file skip).

## M11 — Opt-in history summary for direct invocations — 2026-08-09

### Added

- **`include_history_summary` (spec §7.5)**: direct runs stay cold by
  default; the flag is the one sanctioned way to hand a pinned sub agent
  conversational context — restoring the translator role the planner plays
  for routed dispatch. One summarization call (default model, effort low,
  prompt `app/prompts/history_summary.md`, the planner's capped window),
  recorded as a `summary` step with usage rolled up; the worker receives
  the summary block + the user's verbatim message; fail-open to the cold
  task. Checkpointed task → HITL resume never re-summarizes; retry
  preserves the flag (`runs.include_history_summary`, migration
  `d4f7b2c8e1a9`). Gating: 422 on `/chat` without a target (the
  orchestrator always gets history), 422 on `/invoke` without a
  `conversation_id` (nothing to summarize).
- **Composer checkbox** shown only while a sub agent is pinned AND the
  conversation has a completed run; summarized direct bubbles carry a
  `+ctx` marker.
- Stage 27 acceptance evidence: visibility rules, cold-vs-context
  side-by-side (the cold run cannot recall a number; the context run can),
  the traced summary step, and both 422 gates — plus stages 25/26/12
  re-captured with the checkbox present.

## M10 — Direct sub-agent invocation — 2026-08-09

### Added

- **Direct invocation (spec §7.5)**: a sub agent with `direct_exposure=true`
  can be pinned to handle a request without the planner. Four surfaces, one
  path: `POST /sub-agents/{id}/invoke` (`{message, conversation_id?}` →
  `run_id`), `POST /chat` with `target_sub_agent_id`, the Sub Agents page
  "Invoke →" row action, and the chat composer's target picker
  ("Orchestrator (auto)" + every active exposed agent, with a visible pin
  chip). Every surface creates a persisted run with
  `orchestrator_mode='direct'` + `target_sub_agent_id`.
- **`sub_agents.direct_exposure`** (migration `c9e1f5a2d7b8`): mirrors the
  tools/skills flag — togglable on static records, `direct` chip in the
  table, toggle on both the custom editor and the native card. Static seeds
  ship exposed; the migration backfills existing static agents.
- **`direct` orchestrator mode**: a one-node graph on the run's checkpointer
  thread reuses the ladder executor (`resolve_capability` +
  `execute_resolution`), so native and custom agents run the exact worker
  code paths routed dispatch uses — HITL pause/resume, step recording, and
  a pinned `route` step (rung `native_sub_agent`/`custom_sub_agent`) for
  trace parity. The shared runner tail applies unchanged: formatter on →
  full `answer_ui` treatment; off → raw markdown. Gating (active + exposed)
  is enforced at the API (403/409) and re-checked at execution start;
  `retry` on a failed direct run preserves the pin.

### Changed

- Chat user bubbles for direct runs carry a `→ agent · direct` marker; the
  Runs page mode badge shows `direct`. The Sub Agents page "Test invoke"
  action (message-prefix hint) is replaced by real pinning via `/?target=`.

### Added (seeded native tier)

- **Two new native skills over previously-untagged tools** (spec §9):
  `workspace-auditor` (directory_tree, list_directory_with_sizes,
  search_files, get_file_info — read-only workspace mapping) and
  `workspace-curator` (create_directory, move_file, read_multiple_files —
  tidy, never delete).
- **`workspace-warden`, the first seeded native sub agent** (spec §3.4): a
  hand-written two-stage LangGraph (`audit → curate`) over both skills. The
  graph is code (factory bypassed for construction) but each stage delegates
  to the factory's skill-node semantics — scoped tools, model resolution,
  middleware stack identical to factory-built workers. Registered with
  covered skills by NAME; the seed pass resolves names to registry uuids
  (`_resolve_covers`). Ships exposed, so all four §7.5 direct-invocation
  surfaces work on the native tier out of the box.

## M9 — The formatter: A2UI-first structured answers — 2026-08-08

### Added

- **The formatter role** (spec §7.1): an explicit presentation role beside
  planner/aggregator, with its own Settings section — on/off (off = the
  call never runs, raw renders directly, no artifact exists), model +
  effort (`formatter_model`, null → default model), presentation
  (`a2ui_first` default | `raw_first`), charts toggle, and a user-visible
  coverage flag threshold. Conditional UI: the options exist only while
  the formatter is on.
- **Transformation prompt** (`prompts/formatter.md`): replaces the old
  summarizer with a binding parity contract — preserve every fact,
  number, warning, recommendation; drop only duplication; prose stays in
  markdown `text` components — plus explicit negative examples.
- **Artifact-driven, run-time-frozen rendering**: each artifact carries
  its own `presentation` and `coverage`; history renders by what
  happened at run time, never by current settings. No artifact → raw
  only, no structured toggle. Live runs stream raw tokens and settle
  into their arrangement when the artifact lands.
- **Deterministic coverage metric**: numbers/URLs/code-span retention
  computed in code per artifact, shown as a quiet badge (amber under the
  threshold) — an instrument, never a render gate.
- **Formatter-independent charts**: `render_chart` tool output is
  persisted on `runs.charts` (new column + migration) and rendered with
  the primary answer in every formatter state; tool names now persist on
  trace steps.

## Post-M8 fixes — 2026-08-07

### Fixed

- Never leave a HITL approval card armed after its gate was consumed — a
  stale card could re-submit a decision against an already-resolved gate
  ([PR #3], evidence in `docs/acceptance/22-hitl-stale-card-fix/`).
- Lifecycle scripts made bash 3.2 compatible for stock macOS.
- `log_level` and `otlp_endpoint` settings now have live consumers as
  spec §5b/§10 intend: a PATCH re-applies the log filter and repoints the
  span exporter immediately, stored values override the env bootstrap at
  startup, and never-touched settings leave the env values in charge.
- Run deletion and history purge now also remove the run's LangGraph
  checkpoint rows (orchestrator thread `run_id`, worker threads
  `run_id:*`) — previously they accumulated until `./decom.sh`.
- `docker-compose.yml` header comment updated to acknowledge the
  profile-gated `redis` service added in M7.

### Added

- Update-safe re-runs of `quick-setup.sh`: every prompt defaults to
  "keep what I have" — the provider menu pre-selects the currently keyed
  combination, existing keys are kept on Enter (answer `y` + paste to
  rotate just one), existing Redis provisioning is kept on Enter, and a
  leftover `FAKE_LLM_ENABLED=1` is only removed after asking. Plus
  `--help`/`-h` documenting the interactive walkthrough and every flag.
- Provider-choice setup: `quick-setup.sh` now asks which provider(s) to
  configure (Anthropic / Google / OpenAI, any pair, all three, or keyless
  fake mode), prompts for each selected key, and **verifies every key with
  a free list-models API call** before saving (rejected/unreachable keys
  get a save-anyway escape). Non-interactive: `--providers`,
  `--anthropic-key` / `--google-key` / `--openai-key` (legacy `--key`
  kept as an alias). All provider keys are now optional in spec §13.
- First-boot default-model resolution: if the code default's provider has
  no key when the seed pass first runs, `default_model` is stored as the
  first configured provider's flagship — `claude-sonnet-4-6` →
  `gemini-3.6-flash` → `gpt-5.6-luna` → `fake:scripted`. Explicit
  settings are never touched.
- Documentation suite ([PR #4]): architecture diagrams (C4, ERD, class,
  sequence, state machines, resolution ladder — 20 Mermaid diagrams), ten
  ADRs, API references (REST, SSE contract, workflow DSL, skill format),
  operations guides (runbook, configuration, scaling, troubleshooting,
  data lifecycle), development guides (contributing, local dev, testing,
  code tour, prompt catalog), security posture, observability guide, user
  guide, glossary, and this changelog.

### Added

- Cross-replica registry-cache invalidation over Postgres LISTEN/NOTIFY
  (channel `registry_cache_inv`, origin-tagged payloads, loop-proof;
  dormant on a single node) and optional Redis provisioning in
  `quick-setup.sh --redis` (ADR-0008).

## M8 — Answer surfaces, form gates, and provider campaigns — 2026-08-07

### Added

- Markdown-rendered canonical answers with a collapsible "show structured
  summary" toggle; the structured panel stays expanded in traces as the
  audit surface.
- HITL **form gates**: workflow gates can carry choice and text questions;
  answers are recorded verbatim on the `hitl` step and delivered into
  worker state.
- Themed pure-SVG **charts** (`bar`/`line`/`pie`) in the structured answer,
  data extracted from run content, plus a `render_chart` native tool.
- Per-skill `max_tool_iterations` loop budgets (frontmatter/registry field;
  the static `web-research` skill ships with 20).
- Current model lists for the OpenAI (GPT-5.x family) and Google
  (Gemini flash family) adapters.

### Fixed

- OpenAI reasoning-effort runs are routed through the Responses API —
  current reasoning models reject function tools + `reasoning_effort` on
  `/v1/chat/completions`; the fix lives entirely in the adapter with zero
  consumer changes (ADR-0007). Found live by the stage-19 campaign.

### Verified

- Provider agnosticism proven end to end: five conversations × both
  orchestrator modes on `openai:gpt-5.6-terra` (20/20 turns), a Gemini
  bonus run, and heterogeneous three-provider single runs (Anthropic
  orchestrator × OpenAI planner/sub-agent) — `docs/acceptance/` stages
  19–21.

## M7 — Registry cache layer and progressive-disclosure retrieval — 2026-08-07

### Added

- `RegistryCache` facade for every run-path registry/settings read, with
  live-flippable backends: `bypass` (shipped default, byte-identical direct
  reads — the rollback lever), `memory` (reload-on-dirty), and optional
  `redis`; event-driven invalidation on every write path, no TTLs
  (spec §7.3, ADR-0004). Cache status/refresh API and UI controls.
- Progressive-disclosure top-K retrieval over orchestrator catalogs: BM25 +
  embedding cosine fused with RRF, pinned ids, `use_full_catalog` escape
  hatch; off by default and active only above a per-registry threshold
  (spec §7.4, ADR-0005). Embeddings stored as JSONB via the provider port
  (ADR-0006).

### Fixed

- Settings page reflects `registry_cache_mode` flips in cache status
  immediately; typed redis imports.

## M6 — Compose stack, acceptance, and hardening — 2026-08-05 to 2026-08-06

### Added

- Three-service `docker compose up` stack with prewarmed MCP caches,
  keyless demo mode (fake-provider script control), and lifecycle scripts
  (`quick-setup.sh`, `build.sh`, `start.sh`, `stop.sh`, `decom.sh`).
- Chat experience: live activity ticker, collapsible thinking layout, stop
  button, queued follow-up message, named dispatch rails with steps grouped
  under their sub-agent rails, phonetic callsigns for ephemeral workers,
  and four brand themes (including a themed HITL gate card).
- LLM-as-judge overlap guard on skill/sub-agent saves; delete buttons for
  custom skills and sub agents; Claude Sonnet 5 / Opus 5 in the Anthropic
  adapter (Claude 5 effort maps to adaptive thinking).
- Full manual-UI acceptance evidence: the single-pass 125-screenshot
  campaign plus routing-matrix, determinism, and multi-capability retests
  (`docs/acceptance/` stages 00–17).

### Fixed

- Parallel HITL gates resolve one decision per gate with stale-interrupt-
  aware resume; interrupted tool calls replay on fresh middleware
  instances; plan entries dispatch even when the planner answers part of
  the ask directly.
- Contained tool exceptions in agentic/fallback loops; `spin_worker` strict
  UUID contract with corrective feedback; unique bound-tool names for
  duplicate registry names; schema-repair retry for
  `summarize-and-structure`.
- Reasoning content blocks render as prose (never block reprs); structlog
  renders exception tracebacks; MCP stdio subprocesses inherit network env;
  blank env values treated as unset; MCP server row refreshed after commit;
  workflow preview BFS depth capped (self-loop edge froze the tab);
  theme-correct toggles and A2UI answer cards; queued message and live-run
  view bound to their conversation.

## M5 — Admin command center — 2026-08-05

### Added

- All seven UI pages (Chat, MCP Servers, Tools, Skills, Sub Agents, Runs,
  Settings) in the mission-control design: consistent tables with
  source/kind badges, drawers, workflow DAG builder with validation,
  run trace view, A2UI answer renderer, and the Settings command center.

## M4 — Orchestrator, middleware layer, chat SSE — 2026-08-05

### Added

- Both orchestrator modes, runtime-switchable per run (ADR-0010): graph
  mode (`plan → resolve → dispatch → aggregate` with the deterministic
  capability ladder) and agentic mode (single `create_agent` concierge with
  todos, `spin_worker`, `use_full_catalog`).
- Middleware layer (spec §7.0, ADR-0003): the three registry projections,
  OOB Summarization/call-limit/TodoList middleware, all composed through
  `build_middleware_stack(context)`.
- Chat SSE event contract, HITL interrupt/resume over the Postgres
  checkpointer, run/step recording, and observability (structlog JSON,
  OTel spans, `/metrics`) with the spec §10 label set.

## M3 — Worker factory — 2026-08-05

### Added

- Record → compiled-subgraph worker factory: DAG compile with sequential,
  branch, parallel, and error edges, HITL gate compile, routing, and the
  middleware-backed skill stack.

## M2 — MCP connection manager — 2026-08-04

### Added

- MCP server connect over stdio and streamable HTTP, tool ingestion into
  the registry, `listChanged` re-ingest, health checks, and invoke coverage.

## M1 — Registries, schema, provider layer — 2026-08-04

### Added

- Postgres schema and Alembic baseline for the tri-layer registries
  (mcp_servers, tools, skills, sub_agents), registry CRUD API, static seed
  data with static-record rejection rules (immutable ids; only
  status/exposure togglable).
- The `ModelProvider` port, adapter registry, and `get_model()` single
  entry point with the shared adapter contract test suite (spec §2.1,
  ADR-0002).
- Skill documents as markdown (frontmatter + body), native `.skill.md`
  startup scan, and `{tool:...}` mention validation (ADR-0009).

[PR #1]: https://github.com/maheshyaddanapudi/concierge-agent/pull/1
[PR #2]: https://github.com/maheshyaddanapudi/concierge-agent/pull/2
[PR #3]: https://github.com/maheshyaddanapudi/concierge-agent/pull/3
[PR #4]: https://github.com/maheshyaddanapudi/concierge-agent/pull/4
