# Concierge Agent — Tri-Layer Registry POC

## 1. Goal

Prove that a registry-driven, tri-layer agent architecture works end to end:

```
Orchestrator (planner + parallel dispatch)
  └─> Sub Agents  (persona + workflow DAG over skills)
        └─> Skills (minor persona + tool bindings)
              └─> Tools (ingested from MCP servers)
```

Every tier is backed by a registry with two write paths (static seed at startup, dynamic via admin UI) and one read path (the orchestrator reads registries only — it never distinguishes origin). Success criterion: plug an MCP server from the UI after startup, compose a skill from its tools, compose a sub agent from skills with a branching DAG workflow, and invoke it through chat with a visible run trace — without restarting the app.

Non-goals *as originally scoped*: authentication/authorization, multi-tenancy, production hardening, rate limiting. **Promoted to in-scope by §18.8 (M34, dark by default)** — with `auth_enabled=false` (the default) the original single-user behavior is byte-identical. Secrets management stays env-only throughout; that non-goal stands.

## 2. Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, LangGraph, langchain-mcp-adapters, SQLAlchemy 2 + Alembic |
| LLM | **Provider-agnostic** via LangChain `init_chat_model`. Every model reference is a `provider:model` string (e.g. `anthropic:claude-sonnet-4-6`, `google_genai:gemini-2.5-pro`). POC default `anthropic:claude-sonnet-4-6`. Per-sub-agent model override field |
| DB | Postgres 16 (registries, run history, LangGraph checkpointer) |
| Frontend | React 19 + Vite + TypeScript + Tailwind (React ≥19 required by the official `@a2ui/react` answer-UI renderer). `@xyflow/react` (react-flow) for DAG preview. SSE for streaming |
| Runtime | docker-compose: `db`, `backend`, `frontend`. One command: `docker compose up`. **No message broker, no task queue, no Celery** — runs execute as asyncio tasks inside the single FastAPI process; SSE is plain HTTP; HITL pause/resume rides the LangGraph Postgres checkpointer. Postgres is the only *required* stateful infrastructure. Redis exists solely as an **optional registry-cache backend** (§7.3) behind a compose `redis` profile — the default three-service stack never starts it, and no other subsystem may depend on it. |

### 2.1 Model provider abstraction (non-negotiable basic design)

A first-class provider layer in `backend/app/llm/` — used for **every** provider including Claude and Gemini, never bypassed. Rationale: a custom enterprise provider adapter (its own gateway, its own model list) will be added post-POC; it must plug in without touching any consumer code.

- **Port**: `class ModelProvider(Protocol)` — `provider_id: str`, `is_configured() -> bool`, `list_models() -> list[ModelInfo]`, `get_chat_model(model: str, params: ModelParams | None) -> BaseChatModel`. **`ModelParams` is normalized model configuration**: `{effort: 'none'|'low'|'medium'|'high', temperature, max_output_tokens}` — each adapter maps `effort` to its provider's knob (Anthropic thinking budget, OpenAI reasoning effort, Gemini thinking config); `ModelInfo` declares which params each model supports, and selecting an unsupported combination → 422 at save. The common currency across the whole codebase is LangChain's `BaseChatModel`: a provider adapter's only job is to return one. Everything downstream — LangGraph nodes, planner structured output, router, tools, `usage_metadata` token accounting — depends on `BaseChatModel`, never on a provider.
- **Provider registry**: adapters are code-registered at startup via `@model_provider` decorator scan (same pattern as `native/`), keyed by `provider_id`. Not UI-creatable (adapters are code); Settings lists them read-only with configured/unconfigured status.
- **Built-in adapters (POC)**: `anthropic`, `google_genai`, `openai` — thin wrappers delegating to LangChain `init_chat_model`/provider packages, each gated by its API key env var. A fourth slot is reserved by design for a custom gateway adapter later: implement the port, register, done — zero consumer changes.
- **Single entry point**: `get_model("provider:model") -> BaseChatModel` resolves the prefix against the registry and delegates to the adapter. Every LLM call in the system goes through it — planner, router, aggregator, skill nodes, direct-skill loops, native tools, native sub agents. No provider SDK or LangChain provider package imported anywhere outside `app/llm/`.
- **Embeddings (retrieval, §7.4)**: the port additionally carries `supports_embeddings() -> bool` and `get_embeddings(model: str, texts: list[str]) -> list[list[float]]`; adapters without an embeddings API (Anthropic) report unsupported and raise. The single entry point is `get_embeddings("provider:model", texts)`; embedding model references are the same `provider:model` strings, validated at save. Consumers degrade gracefully: no configured embedding model → retrieval runs lexical-only.
- **Model references**: always `provider:model` strings (settings, per-agent overrides, trace labels). Default `anthropic:claude-sonnet-4-6`. Selecting a model whose adapter reports unconfigured → 422 at save.
- **Neutrality rules**: structured outputs via LangChain's structured-output/tool-calling abstraction only; token accounting via `usage_metadata` only; prompts in `backend/app/prompts/` provider-neutral.
- **Adapter contract tests**: one shared pytest suite asserting the port contract (list/configure/get, **normalized `ModelParams` mapping incl. effort→provider knob**, tool-calling round-trip, structured output, usage metadata population against a fake); every registered adapter must pass it — this suite is what makes the future custom adapter safe to drop in.

Repo layout:

```
concierge-agent/
├── spec.md
├── docker-compose.yml
├── backend/
│   ├── app/
│   │   ├── api/          # routers per registry + chat + runs
│   │   ├── models/       # SQLAlchemy models
│   │   ├── schemas/      # Pydantic schemas
│   │   ├── mcp/          # MCP connection manager
│   │   ├── factory/      # worker factory (record -> compiled subgraph)
│   │   ├── llm/          # ModelProvider port, provider registry, adapters (§2.1)
│   │   ├── native/       # @native_tool / @native_skill / @native_sub_agent registrations
│   │   ├── orchestrator/ # planner + dispatch graph
│   │   └── seed/         # static seed data
│   └── tests/
└── frontend/
    └── src/
        ├── pages/        # Chat, McpServers, Tools, Skills, SubAgents, Runs, Settings
        └── components/
```

## 3. Data Model

Common columns on all registry tables: `id (uuid, immutable)`, `name`, `description`, `source ('static'|'dynamic')`, `status ('active'|'inactive'|'error')`, `created_at`, `updated_at`, `deleted_at (soft delete)`. Each tier additionally carries a `kind` discriminator (defined per tier below); `source` + `kind` + tier are attached to every trace span and log line (§10).

### 3.1 mcp_servers
- `transport`: `'stdio' | 'http'`
- stdio: `command`, `args (jsonb)`, `env (jsonb)`
- http: `url`, `headers (jsonb)`
- `last_connected_at`, `last_error`

### 3.2 tools
- `kind`: `'mcp' | 'native'`
- `mcp_server_id` (FK, required when `kind='mcp'`, null when `kind='native'`)
- `tool_name` (name as exposed by the MCP server, or the native provider registration name)
- `native_ref` (text, nullable) — for `kind='native'`: dotted path of the registered provider entry (e.g. `native_tools.summarize_graph`)
- `tool_key` (string, unique) — human-facing identifier. Defaults to `{server_name}.{tool_name}` for MCP tools (collision-safe across servers exposing the same tool name) and the registration name for native tools; auto-generated uuid string only if neither yields a unique key. **Editable later.** Internal references (skill_tools join) always use the immutable `id`, so renaming `tool_key` never breaks bindings.
- `direct_exposure (bool, default false)` — when true, the orchestrator may call this tool itself, without a sub agent. Note: direct tool calls carry no skill persona — the tool runs under the orchestrator's own prompt.
- `input_schema (jsonb)` — JSON Schema from `tools/list` (mcp) or derived from the callable/subgraph input schema (native)

### 3.3 skills
- `kind`: `'native' | 'custom'` — **native skills are markdown files** (`backend/app/native/skills/*.skill.md`, YAML frontmatter + body) scanned into the registry at startup; **custom skills are the same document shape** authored in the UI and stored in the registry. One format, two homes.
- **Skill document** = frontmatter (`name`, `description`, `persona`, `tools: [tool_keys]`, `direct_exposure`) + markdown body (`instructions`).
- `instructions (text, markdown)` — the body: free-form, typically a multi-step process. **Soft workflow**: steps guide the LLM inside a single tool-loop node and are not machine-enforced (the hard, machine-executed workflow is the sub agent DAG, §3.5). A step need not tie to any tool — pure reasoning/formatting steps are valid. Steps may reference bound tools inline via `{tool:server.tool_name}` mentions; save validates every mention resolves to a bound tool.
- `model (text, nullable)` + `model_params (jsonb, nullable)` — optional per-skill override (`provider:model` + normalized params, §2.1): a heavy research skill can run a stronger model at high effort while a formatting skill runs a cheap one. Null inherits from the invoking sub agent, then defaults.
- `persona (text)` — the "minor persona": short instruction block injected when this skill's node runs
- `skill_tools` join table: `skill_id`, `tool_id`. **Binding = availability, strictly**: the skill's loop receives exactly its bound tools and nothing else — no ambient tools, no cross-skill visibility, in every execution context including orchestrator fallback (§7.0). A tool can belong to many skills; a skill can tag many tools, including system-seeded static ones.
- `direct_exposure (bool, default false)` — when true, the orchestrator may execute this skill inline (skill persona + instructions + bound tools injected into an orchestrator tool-loop step) without spinning a sub agent.
- `max_tool_iterations (int, nullable)` — per-skill override of the loop budget: this skill's tool loop may run this many iterations before the node fails (error-edge semantics unchanged). Null inherits the `max_tool_iterations` setting. Rationale: research-class skills legitimately need deeper loops (the static `web-research` skill ships with 20) without raising the global default.
- **Strict id references**: every request that binds or invokes a skill (workflow nodes, skill endpoints) must carry an explicit `skill_id`; no name-based resolution. Missing/unknown `skill_id` → 422 rejected.

### 3.4 sub_agents
- `kind`: `'native' | 'custom'`
  - **native**: a hand-written compiled LangGraph graph registered at startup (`@native_sub_agent(name, description, covers_skill_ids=[])` scan). `covers_skill_ids` declares which registry skills this agent can service — used by capability resolution (§7). Registry stores its card (name, description, `native_ref`); the worker factory is bypassed — invocation calls the registered graph directly. Must accept the standard state schema (`{messages, task, node_outputs}`) and use the shared Postgres checkpointer; HITL/interrupts are permitted (native sub agents run top-level, not tool-wrapped). `workflow` is null.
  - **custom**: registry-composed — persona + workflow DAG over skills (the worker-factory path). The DAG is the **hard**, machine-executed workflow across skills; multi-step processes inside a skill's `instructions` are soft guidance within one node (§3.3). Custom sub agents are built from the skills pool; each skill in the picker surfaces its bound tools so the full skills-vs-tools pool is visible at composition time.
  - **Declarative static custom agents (`.agent.md`)**: mirroring skills' "one format, two homes", a static custom sub agent can be authored as a markdown file in `backend/app/native/sub_agents/` — YAML frontmatter (`name`, `description`, `persona`, optional `model`/`model_params`, `direct_exposure`, and the §3.5 `workflow` with the one sugar that skill nodes may reference skills **by name**, resolved to registry uuids at seed time) plus a markdown body that is non-functional documentation. The seed scan upserts by `(name, source='static')` with `native_ref='file:<filename>'` as provenance: definition fields always follow the file; a user's `status`/`direct_exposure` toggles survive reseeds (a file fixed after an error flips `error → active`); a removed file marks its row `inactive`, never deletes it. Files are validated at seed with the same structural + factory-compile checks the API applies at save — an invalid file lands as `status='error'` (visible in UI, logged) rather than crashing boot or silently vanishing. Code-registered native agents and UI-authored dynamic agents are unaffected; this is a third *authoring* path onto the same registry row.
- `persona (text)` — top-level persona for the worker (custom only)
- `model (text, nullable)` + `model_params (jsonb, nullable)` — `provider:model` string + normalized params (§2.1) override; null falls back to `default_model` / `default_model_params` settings. **Resolution order for any skill node: skill override → sub agent override → settings defaults.**
- `workflow (jsonb)` — DAG, schema below (custom only)
- `sub_agent_skills` join table derived from workflow at save time (for badge queries)
- `direct_exposure (bool, default false)` — when true, the sub agent can be **invoked directly** (§7.5): pinned from chat or called via `POST /sub-agents/{id}/invoke`, bypassing the planner/routing decision. Mirrors the tools/skills flag exactly, including the static-record rule: togglable on static records while the definition stays immutable. Static seed agents ship with it enabled so direct invocation works out of the box.

### 3.5 Workflow DAG schema (jsonb)

```json
{
  "nodes": [
    {"id": "n1", "type": "skill", "skill_id": "<uuid>", "instructions": "optional per-node addendum"},
    {"id": "n2", "type": "hitl",  "prompt": "Approve sending the summary?"},
    {"id": "n3", "type": "skill", "skill_id": "<uuid>"}
  ],
  "edges": [
    {"from": "START", "to": "n1"},
    {"from": "n1", "to": "n2", "condition": "if research found results"},
    {"from": "n1", "to": "END", "condition": "if nothing found"},
    {"from": "n2", "to": "n3"},
    {"from": "n3", "to": "END"}
  ]
}
```

Rules:
- Node types: `skill`, `hitl`. `skill` nodes reference a skill registry record. `hitl` nodes pause the run and wait for chat approval.
- **Form gates**: a `hitl` node may carry `questions: [{id, prompt, kind: 'approve'|'choice'|'text', options?: [string]}]` alongside (or instead of) the plain `prompt`. The chat renders every question on one card — choice chips, text fields, approve toggles — answered together with a single Submit. Validation at save: question ids unique and non-empty, `choice` requires ≥2 options. A gate must never be un-answerable: malformed question specs degrade to the plain approve/deny card.
- Branching: multiple outgoing edges from one node with `condition` strings. Conditions are natural-language; a router step (LLM call with the node's output + condition list) selects the edge. Unconditional single edge = direct transition.
- **Error edges**: an edge may carry `"on": "success" | "error"` (default `success`). When a node fails (tool/MCP error, LLM error), execution routes via its `error` edge if one exists — the failed node's error text lands in state for the downstream node. No error edge → node failure fails the run (step + run marked `failed`, error surfaced in chat). Router/condition evaluation applies only to `success` edges.
- Parallelism: multiple unconditional outgoing edges from one node fan out (LangGraph `Send`); a node with multiple incoming edges joins **when all *reachable* incoming edges have completed** — branches not taken (unselected conditions, error paths bypassed) are marked unreachable at route time so joins never deadlock.
- Validation at save (reject, don't warn): exactly one `START`, at least one path to `END`, no cycles, all `skill_id`s resolve to active skills, all node ids unique, edges reference existing nodes, at most one `error` edge per node.

### 3.6 conversations / runs / run_steps
- `conversations`: `id`, `title (auto from first message)`, `created_at`, `updated_at`. Chat is multi-turn: every run belongs to a conversation, and the planner receives the **full conversation history** (all prior user messages + final answers in the conversation) as context.
- `runs`: `id`, `conversation_id (FK)`, `chat_message`, `plan (jsonb)` (planner output), `snapshot (jsonb)` — resolved config frozen at dispatch: for each capability used, the persona/workflow/model/tool definitions as they were at run time, so later registry edits never rewrite trace history, `orchestrator_mode ('graph'|'agentic'|'direct')` — how the run executed: the orchestrator mode setting at creation, or `direct` for a pinned sub-agent invocation (§7.5), `target_sub_agent_id (uuid, nullable)` — set only on `direct` runs: the sub agent the user pinned, `include_history_summary (bool, default false)` — §7.5 opt-in: this direct run summarized conversation history into the worker's context, `answer_ui (jsonb, nullable)` — the formatter's structured artifact (§7.1 `answer_ui` event; persisted so conversation reload and the Runs page re-render it; the payload carries its own `presentation` and `coverage`, so history renders by what happened at run time, never by current settings), `charts (jsonb, nullable)` — chart specs produced by the `render_chart` native tool during the run, rendered with the primary answer in every formatter state, `status ('running'|'paused_hitl'|'completed'|'failed'|'cancelled')`, `started_at`, `finished_at`, `total_input_tokens`, `total_output_tokens`
- `run_steps`: `id`, `run_id`, `parent_step_id (nullable — nested steps, e.g. inside a native subgraph tool)`, `sub_agent_id`, `node_id`, `step_type ('plan'|'route'|'skill'|'hitl'|'tool_call'|'aggregate'|'summary')`, `input (jsonb)`, `output (jsonb)`, `model`, `input_tokens`, `output_tokens`, `started_at`, `finished_at`, `error`

### 3.7 app_settings

Key-value store (`key`, `value jsonb`, `updated_at`) read live at runtime — changes apply to the next run, no restart. Keys: `orchestrator_mode ('graph'|'agentic')`, `orchestrator_full_fallback_enabled (default true)`, `default_model`, `default_model_params`, `planner_model`, `planner_model_params`, `aggregator_model`, `aggregator_model_params`, `max_parallel_dispatch`, `max_plan_steps`, `max_tool_iterations` (per skill-node tool loop; exceeded → node fails, error-edge semantics apply), `dynamic_worker_fallback_enabled`, `direct_exposure_cap_warning`, `formatter_enabled (default true — whether the formatter model call runs at all; off = raw answer rendered directly, no structured artifact produced)`, `formatter_presentation ('a2ui_first'|'raw_first', default 'a2ui_first')`, `formatter_model` / `formatter_model_params` (nullable — null falls back to `default_model`, single hop, like planner/aggregator), `formatter_coverage_flag_threshold (default 90 — visual flag only, never a render gate)`, `answer_ui_charts_enabled (default true)`, `mcp_health_interval_s`, `log_level`, `langsmith_enabled`, `langsmith_endpoint`, `langsmith_project`, `otlp_endpoint`, `registry_cache_mode ('bypass'|'memory'|'redis', default 'bypass', §7.3)`, `retrieval_enabled (default false, §7.4)`, `retrieval_threshold (default 30)`, `retrieval_top_k (default 10)`, `embedding_model (nullable 'provider:model', §2.1)`, plus the §16 memory keys: `memory_enabled (default false — master switch; off is byte-identical to pre-§16 behavior)`, `memory_extraction_enabled (default true — gates the L2 write pipeline when memory is on)`, `memory_reflection_enabled (default false)`, `procedural_learning_enabled (default false)`, `memory_injection_budget_tokens (default 1200)`, `memory_pinned_budget_tokens (default 400)`, `memory_recall_top_k (default 6)`, `memory_score_floor (default 0.35)`, `memory_extraction_model` / `memory_extraction_model_params` (nullable — null falls back to `default_model` at effort low), `memory_half_life_days (default 30.0)`, `memory_idle_minutes (default 10)`, `memory_digest_compact_days (default 14 — run-digests older than this fold into per-conversation period digests, §16.7)`, `memory_forget_enabled (default false — M44 §16.1 durable forgetting; off is byte-identical: user deletes stay physical and mode=forget is a 422 naming this key)`, `memory_forget_similarity (default 0.85, range 0.5–1.0 — cosine threshold for semantic re-admission suppression, calibrated against a live-measured paraphrase at 0.876; hash-only when no embedding model is configured)`, `memory_admission_min_confidence (default 0.5, range 0.0–0.9 — the §16.2 admission-gate confidence floor, promoted from the constant it replaces under the M40 pattern; the M47 learner moves it in ±0.05 steps clamped to [0.5, 0.9])`, `memory_quarantine_kinds (default [] — machine writes of a listed kind land in the §16.2 review queue instead of activating; user-stated writes are never routed)`, `memory_extraction_learning ('off'|'propose'|'auto', default 'off' — M47: the tombstone-informed extraction tuner, the second consumer under the §17.7 feedback-consumer rule)`, plus the M48 job gates — **no behavior the system performs on its own may be unswitchable** (§3.7.1), each defaulting to the behavior it replaces so the promotion is byte-identical: `memory_decay_enabled (default true — the §16.2 access-recency decay sweep that expires unpinned rows)`, `memory_contradiction_enabled (default true — the sweep that quarantines duplicate active entity_keys)`, `memory_communities_enabled (default true — the §18.6 community rebuild, which makes LLM summarization calls per changed community)`, `memory_compaction_enabled (default true — the §16.7 digest fold, which HARD-DELETES the folded run-digests; the one consolidation job with irreversible effect)`, plus the §17 ambient keys: `ambient_enabled (default false — master switch; off is byte-identical)`, `ambient_max_routines (10)`, `ambient_runs_per_day (50)`, `ambient_routine_events_per_hour (20)`, `ambient_idle_minutes (10 — subsumes memory_idle_minutes)`, `ambient_hitl_timeout_h (24)`, `ambient_digest_times (default ["09:00","17:00"] local)`, `ambient_notification_budget_per_day (3)`, `ambient_quiet_hours (default ["22:00","07:00"])`, `ambient_interrupt_threshold (4)`, `ambient_wakeups_per_routine_per_day (100)`, `ambient_escalation_budget_per_day (10)`, `ambient_learning_mode ('off'|'auto'|'propose', default 'off', §17.7)`, `ambient_precision_rule_enabled (default true — gates the §17.3 rule-based precision auto-downgrade, the static-policy feedback consumer active while learning is off; false = feedback is still captured but never re-tiers a category; true is byte-identical to pre-M43c behavior)`, plus the §18 keys: `ambient_channels (per-tier delivery channel routing, default {} = in-app only, §18.4)`, `ambient_pursuit ('off'|'away'|'always', default 'always' — whether the external channels named by that routing actually fire for a batch whose in-app broadcast reached nobody, §18.4/§17.5; 'always' is the pre-M41 presence-blind behavior, so the default is byte-identical)`, `ambient_salience_mode ('off'|'propose'|'auto', default 'off' — the §17.5 M42 content-salience pass over unseen tier ≤1 deliveries; off is byte-identical, propose queues verdicts for approval, auto applies them)`, `ambient_salience_min_urgency (default 3, range 1–5 — the deterministic prefilter floor before the judge is ever called)`, `ambient_salience_learning ('off'|'propose'|'auto', default 'off' — M45: the salience tuner, the first consumer of the M43b judge_reward ledger, entering under the §17.7 feedback-consumer rule)`, `ambient_salience_model` / `ambient_salience_model_params` (nullable — null falls back to `default_model`, like planner/aggregator/formatter), `ambient_anticipation_enabled (default true — M48: gates the §18.1 idle-time anticipation job. This is the only feature that INITIATES contact unprompted, so it gets an explicit switch rather than relying on the hit-rate floor to learn its way to silence; true is byte-identical to pre-M48)`, plus `evals_enabled (default true — M48: mounts the §15 eval surface; the feature is passive, so the gate exists to remove surface area, not to change behavior)`, plus the §19 A2A keys: `a2a_enabled (default false — master switch; off is byte-identical)`, `a2a_card_refresh_interval_s (default 300)`, `a2a_task_timeout_s (default 120 — in-run wait budget before park-or-error, §19.5)`, `a2a_poll_interval_s (default 60 — parked-task recheck cadence, §19.6; tick-bounded: the ambient leader tick invokes the poller, which no-ops until this interval has elapsed since its last poll, so the effective cadence is max(tick, interval))`, `a2a_max_parked (default 20 — beyond it budget expiry is a plain tool error)`, plus the M40 config-hardening keys: `ambient_tick_interval_s (default 60, min 15 — the ambient scheduler tick cadence; evaluators, drain heartbeat, and the parked-task poller all ride it)`, `rate_limit_burst (default 120)` / `rate_limit_per_s (default 10)` (the §18.8 token bucket, read live per refill), `overlap_threshold_percent (default 70, range 0–100 — the §4 overlap-guard gate; 100 effectively disables the dialog, 0 flags every save)`, `run_stall_after_s (default 300, min 60 — the §17.4 H3 reaper window before a silent ambient run is marked stalled)`, `agentic_recursion_limit (default 100, range 10–500 — the agentic loop's LangGraph recursion budget; the model-call limit stays derived from max_tool_iterations)`, `a2a_http_timeout_s (default 15, min 1 — the shared A2A HTTP client timeout, applied on the manager's next client build)`, `a2a_fence_max_chars (default 8000, min 500 — cap on fenced remote output, §19.5)`. Anthropic API key stays env-only — never stored in DB or shown in UI, even in a POC; the Redis URL likewise (`REDIS_URL` env, §13).

### 3.7.1 The switchability rule (M48)

Two invariants, both auditable from code rather than asserted:

1. **No behavior the system performs on its own may be unswitchable.** Any
   job, sweep, or judge that runs without a human asking for it — on a
   timer, on idle, on a tick — has a named gate in §3.7. A master switch
   covering a family is not sufficient when the family members have
   materially different effects: expiring memories, hard-deleting digests,
   spending tokens on summarization, and composing an unprompted briefing
   are four different consequences and take four different switches. The
   M48 audit found four consolidation jobs, the anticipation job, and the
   §15 eval surface running with no gate of their own; all six now have one.

   **The gate is enforced inside the behavior, never only at its caller.**
   These jobs are documented as directly awaitable (tests, the experiment
   harnesses, any future caller), so a check that lives only in the
   scheduler is a check every other path walks past. The scheduler reads
   the same `JOB_GATES` map — one source of truth, no drift — purely to
   skip taking an advisory lock for work that would return immediately.
   A structural test asserts every scheduled job appears in that map and
   that every entry names a real §3.7 key, so the next job cannot ship
   ungated and a renamed key cannot silently orphan a gate.
2. **Every §3.7 key has a control in the section of §8.7 that owns it**
   — the M43 rule, restated because M48's audit found two keys
   (`memory_digest_compact_days`, `memory_community_budget_tokens`) that
   were API-validated and unreachable in the UI. Settings coverage is
   asserted by test, not by review: the suite fails if a key exists with
   no control.

**Corollary — a setting that reads as "off" must be off.** A value whose
plain reading is "this does nothing" may not leave work running:
`memory_community_budget_tokens = 0` silenced the injected section while
the hourly rebuild kept making LLM calls, so 0 now short-circuits the
rebuild as well. Where a configuration is legal but degraded rather than
off, the UI says so at the control (the `memory_forget_enabled`-without-
`embedding_model` case: suppression falls back to exact-hash matching and
paraphrases are re-admitted — legal, silent, and now labelled).

## 4. Registry API

REST, JSON, `/api/v1`. All list endpoints support `?include_deleted=false&source=&q=`.

| Resource | Endpoints |
|---|---|
| MCP servers | `GET/POST /mcp-servers`, `GET/PATCH/DELETE /mcp-servers/{id}`, `POST /mcp-servers/{id}/reconnect`, `POST /mcp-servers/{id}/refresh-tools` |
| Tools | `GET /tools`, `GET/PATCH /tools/{id}` (PATCH: description/status only — schema is server-owned), `GET /tools/{id}/skills` (reverse lookup for badges) |
| Skills | `GET/POST /skills`, `GET/PATCH/DELETE /skills/{id}`, `GET /skills/{id}/sub-agents`, `POST /skills/check-overlap` (pre-save judge, below) |
| Sub agents | `GET/POST /sub-agents`, `GET/PATCH/DELETE /sub-agents/{id}`, `POST /sub-agents/{id}/validate` (dry-run factory compile), `POST /sub-agents/{id}/invoke` (`{message, conversation_id?, include_history_summary?}` → `run_id` — direct invocation, §7.5; 403 unless `direct_exposure`, 409 unless active, 422 if the summary flag rides without a `conversation_id`), `POST /sub-agents/check-overlap` (pre-save judge, below) |
| Chat | `GET/POST /conversations`, `GET /conversations/{id}` (messages + runs), `POST /chat` (`{conversation_id, message, target_sub_agent_id?, include_history_summary?}` → `run_id`; the optional target pins the run to that sub agent — direct invocation, §7.5, same gating as `/invoke`; the summary flag is 422 without a target), `GET /chat/stream/{run_id}` (SSE), `POST /runs/{run_id}/hitl` (`{"decision": "approve"|"deny", "note": "..."}`) |
| Runs | `GET /runs`, `GET /runs/{id}` (includes ordered steps), `POST /runs/{id}/cancel`, `POST /runs/{id}/retry` (re-plan from original message), `DELETE /runs/{id}` |
| Settings | `GET /settings`, `PATCH /settings` (partial key updates), `GET /hitl/pending` (all paused runs), `POST /seed/reload` (idempotent re-seed) |

Cross-cutting rules:
- `source=static` records: content PATCH/DELETE return 403 with reason; the single exception is `status` (and `direct_exposure` where applicable), which is togglable on static records — the command center can switch anything off without editing its definition. Rendered read-only-with-toggles in UI.
- Referential integrity enforced at save time: deleting a skill referenced by an active sub agent → 409 listing dependents. Same for tools in skills and MCP servers with tools in skills.
- Every mutating endpoint bumps `updated_at`; registry reads are always live from DB (no caching layer in POC).

**Overlap guard (LLM-as-judge, advisory).** Before a skill or sub agent is created or updated, the UI calls the matching `check-overlap` endpoint with the draft (`exclude_id` set on updates so a record never matches itself). The judge — an LLM call through the standard provider port, prompt in `app/prompts/overlap_judge.md`, structured `OverlapVerdict` output — compares the draft by purpose against existing records: skills against active skills **and tools**, sub agents against active sub agents **and skills**. At ≥70% overlap the response flags it with the match (type/id/name) and reasoning, and the UI shows a confirm dialog: **save anyway** or **cancel and use the existing match**. The check is advisory only: the save endpoints stay unguarded, and judge failure (no key, provider error) fails open with `overlap: false` plus a reasoning note — infrastructure trouble must never block registry writes. Tools are deliberately exempt: MCP ingest is dynamic, and two servers legitimately exposing similar tools is not an error.

## 5. MCP Connection Manager

Singleton service owning one client session per active `mcp_server` record.

- **Register (dynamic)**: POST creates record → manager connects (stdio: spawn `command args` inside backend container; http: streamable-HTTP client) → `tools/list` → upsert `tools` rows (`source` inherited from server record) → status `active`. Connection failure → status `error`, `last_error` set, record kept for retry.
- **listChanged**: subscribe to tool-list-changed notifications; on receipt re-run `tools/list` and reconcile (add new, mark removed tools `inactive`).
- **Health**: ping loop every 30s; failures flip status to `error`; `reconnect` endpoint retries.
- **Startup**: manager loads all non-deleted `mcp_servers` (static seed + previously added dynamic ones) and connects each. Dynamic records therefore survive restarts — DB is the source of truth, not memory.
- **Invocation**: skills resolve tool bindings → manager exposes each bound tool as a LangChain tool object (via langchain-mcp-adapters) for the worker factory.

## 5b. Native Tool Provider

Native tools are code-defined tools that live in the backend — plain Python callables or **compiled LangGraph subgraphs exposed as tools** (`graph.as_tool()` / a wrapper invoking the subgraph). They register into the same `tools` registry (`kind='native'`) and are bindable to skills exactly like MCP tools.

- **Registration**: at startup, the provider scans `backend/app/native/` for entries decorated with `@native_tool(name, description)` (and `@native_sub_agent`, §3.4); native skills load from `native/skills/*.skill.md` (§3.3). Each entry is upserted into `tools` (`kind='native'`, `source='static'`, `native_ref` = dotted path, `input_schema` derived from the callable signature / subgraph state schema). Entries removed from code are marked `inactive` on next startup.
- **No dynamic native tools**: native tools cannot be created from the admin UI — that would mean accepting executable code at runtime. UI shows them with a `native` badge; description/status editable, nothing else.
- **Invocation**: worker factory resolves tool bindings by `kind` — `mcp` via the MCP manager, `native` via the provider's in-process callable. Same LangChain tool interface either way; skills and sub agents are agnostic.
- **Guardrails (validated at startup registration and at skill/sub-agent save)**:
  1. **No HITL inside native subgraphs** — LangGraph interrupts do not propagate cleanly out of a tool call; a native subgraph containing `interrupt()` is rejected at registration.
  2. **No registry sub agent wrapping** — a native tool may not invoke a registry sub agent (prevents cycles: sub agent → skill → tool → same sub agent). Native subgraphs are self-contained; composition across registry sub agents belongs to the orchestrator planner, not the tool layer.
  3. **Token + trace capture** — native subgraph invocations report LLM usage via callback; recorded as a `tool_call` step with nested child steps (`run_steps.parent_step_id`) so trace and token rollups stay accurate.
- **Seed**: one static native tool, `summarize-and-structure` (single-node subgraph: LLM call that converts raw text into a structured JSON summary), bound into the `web-research` skill — proves the subgraph-as-tool path end to end alongside MCP tools in the same skill.

**Built-in `render_chart` native tool**: validates and normalizes `{kind: 'bar'|'line'|'pie', title?, labels: [string], series: [{name?, values: [number]}]}` and returns the normalized spec — a capability any tier can use to propose a chart from data it actually holds. Enable/disable is pure registry mechanics: `direct_exposure` gates the orchestrator, skill bindings gate sub agents and dynamic workers, `status` is the global kill switch. Display happens via the §7 `chart` answer-UI component.

## 6. Worker Factory

`build_worker(sub_agent_record) -> CompiledStateGraph`. The load-bearing component.

- **Compilation**: workflow DAG → hand-built LangGraph `StateGraph`. **Each `skill` node is implemented with LangChain's `create_agent`** (the standard tool-loop constructor): model from `get_model()`, tools from the skill's bindings, system prompt from the assembly order below, loop bounded by `max_tool_iterations`. **The DAG shell is never `create_agent`** — routing, conditional/error edges, `Send` fan-out, joins, and `interrupt()` are explicit StateGraph construction, because that's the deterministic envelope. `skill` node → agent node: system prompt assembled in order (1) sub agent persona, (2) skill persona, (3) skill `instructions` (markdown body), (4) node `instructions` addendum from the DAG, (5) tool-usage guidance; tools = the skill's bound tools resolved by kind (`mcp` → MCP manager, `native` → native tool provider). `hitl` node → LangGraph `interrupt()` carrying the node's `prompt`. Conditional edges → router function (LLM call: node output + condition strings → chosen edge). Fan-out edges → `Send`; joins via reducer state.
- **State**: `{messages, node_outputs: dict[node_id, output], task}`. Each skill node appends its output; downstream nodes receive prior outputs in context. **M3 constraint**: state reducers must be order-insensitive — parallel branches complete in nondeterministic order, and joins must converge to the same state regardless of interleaving (keyed dict merges, no order-dependent appends).
- **Checkpointing**: Postgres checkpointer keyed by `run_id` — required for HITL pause/resume.
- **Compile-time = save-time**: `POST /sub-agents/{id}/validate` runs full compilation without invocation; save rejects records that fail compile. The orchestrator never selects a broken worker.
- **Caching**: compiled graphs cached in-process keyed by `(sub_agent_id, updated_at)`; a registry update naturally invalidates via the changed timestamp.

## 7. Orchestrator

Two selectable orchestrator modes share every subsystem (registries, ladder policy, factory, MCP manager, provider layer, tracing); `app_settings.orchestrator_mode: 'graph' | 'agentic'` (default `graph`) switches per run.

### 7.0 Middleware layer (the sync backbone, both modes)

Three custom middlewares wrap the registries and are the **only** path by which capabilities reach any `create_agent` instance in the system:

- **ToolsRegistryMiddleware** — dynamic-tools hook: resolves tool objects live from the tool registry (via MCP manager / native provider) at each model call. A reconnected MCP server or newly ingested tool is visible on the next loop iteration, no rebuild.
- **SkillsRegistryMiddleware** — injects exposed skills: summaries into the system prompt (dynamic prompt hook) and each exposed skill as a callable capability whose handler runs the inline skill loop.
- **SubAgentsRegistryMiddleware** — deepagents-style: exposes dispatch as tools built live from sub agent cards; the handler is the resolution-ladder executor (validate → snapshot → invoke compiled graph on the shared checkpointer). `interrupt()` raised inside the dispatched graph propagates to the parent for HITL; **dispatch handlers are idempotent** because resume replays the tool call.
- Standard middlewares on every `create_agent` instance (skill nodes, rung-1 loops, agentic orchestrator): **SummarizationMiddleware** (context compaction — also applied to conversation-history assembly, closing the long-conversation gap) and the built-in call-limit middlewares implementing `max_tool_iterations` instead of custom counting.

**Middleware policy**: prefer out-of-box LangChain middleware configured via its options; compose or subclass an existing hook when that suffices; write custom middleware only when nothing out-of-box can be configured to fit (the three registry middlewares qualify — nothing OOB projects a Postgres registry). Never duplicate an OOB middleware's concern.

**Composition (one helper, `build_middleware_stack(context)`, used everywhere)**:
- **Skill loops** (nodes in custom sub agents, rung-1 inline, ephemeral workers, skills invoked from fallback): SummarizationMiddleware + call-limit middlewares + ToolsRegistryMiddleware in **scoped mode** — resolves live tool objects for the skill's bound ids only. Never SkillsRegistry or SubAgentsRegistry middleware here: a skill cannot see other skills, agents, or unbound tools (§3.3 invariant enforced structurally).
- **Agentic orchestrator**: all three registry middlewares (exposure-gated; full-catalog in fallback) + TodoListMiddleware + SummarizationMiddleware + limits.
- **Graph orchestrator**: no middleware on the shell (it isn't `create_agent`); its rung-1 loops and fallback loop use the stacks above.
- **Native sub agents**: contract requires only state schema + checkpointer; authors using `create_agent` internally may attach the shared stacks via the same helper.
Registry middlewares are stateless projections over Postgres (fresh read per model call), so instances are cheap and share nothing — reuse is by class + config, never by shared state.

**Sync invariant**: registries are the single source of truth. Registry reads flow through the registry cache layer (§7.3), whose freshness contract is **event-invalidated**: every registry write path invalidates before the write returns, so visibility of a new MCP server, tool, skill, or sub agent remains: next model call in any middleware-backed loop, next run for graph-mode plan context. In `bypass` mode this degenerates to the literal fresh-read-per-model-call behavior. Compiled graphs remain separately cached (keyed by `updated_at`, self-invalidating). Manual refresh exists in the UI as an operator override (§8.7), never as a correctness requirement.

**Self-service fallback (full catalog)**: when semantic routing fails — the planner cannot produce a confident capability match, or every plan entry fails resolution — and `orchestrator_full_fallback_enabled` is true, the orchestrator handles the query itself instead of spinning a worker: a `create_agent` loop whose registry middlewares switch to **full-catalog mode** — ALL active tools and ALL active skills become callable, exposure flags ignored. This is the deliberate backup for bad descriptions: routing needs good prose, the fallback doesn't. Two invariants hold even here: a skill invoked from fallback still runs with only its bound tools (isolation is never suspended), and every fallback capability use is traced with route rung `fallback`. In agentic mode the orchestrator's base capabilities stay exposure-gated; a `use_full_catalog` escalation tool unlocks full-catalog mode mid-loop, logged as the same fallback route. Full catalog in context is the token cost we normally avoid via progressive disclosure — acceptable precisely because this path only runs when disclosure-based routing has already failed.

### 7.1 Graph mode (default)

A hand-built LangGraph `StateGraph` — **not** `create_agent`: `plan → resolve → dispatch (parallel) → aggregate`. The planner is a structured-output call, resolution is pure code, dispatch is graph mechanics — none of that is a tool loop. `create_agent` appears in the orchestrator only inside rung-1 direct-skill execution, where the inline skill loop is itself a `create_agent` instance (persona + instructions + bound tools).

- **Plan**: progressive disclosure — prompt contains compact registry summaries only: sub agent cards (name + description + skill names) plus, listed separately as directly usable capabilities, all `direct_exposure=true` tools and skills. Planner LLM outputs JSON: `[{capability: {type: 'direct_tool'|'direct_skill'|'sub_agent'|'spin_worker', id|skill_ids}, task, depends_on: []}]`.
- **Capability resolution ladder** — for each capability the plan needs, in order:
  1. **Direct**: the tool/skill has `direct_exposure=true` → orchestrator executes it itself (direct tool = plain tool call; direct skill = inline tool-loop step with the skill persona).
  2. **Native sub agent**: a registered native agent whose `covers_skill_ids` includes the needed skill → dispatch to it.
  3. **Custom sub agent**: a custom agent whose workflow includes the skill (via `sub_agent_skills`) → dispatch to it.
  4. **Ephemeral dynamic worker**: none of the above → the worker factory builds a worker on the fly over one **or several** skills (`skill_ids[]`, executed as a sequential chain in the given order; first skill's persona leads, later personas append per node). Not persisted to any registry; traced with sub-agent-tier `kind='dynamic'`, `entity_id=null`, skill ids recorded on the steps. Named per run with a sequential phonetic callsign plus its composition — `worker-alpha (web-research+file-ops)` — so parallel ephemeral workers stay distinguishable across rails, ticker, and trace. Discarded after the run. **Exposed skills only**: an ephemeral worker composes exclusively `direct_exposure=true` skills — a non-exposed skill_id is rejected with a `ResolutionError` in both modes, whatever the caller (planner entry or agentic `spin_worker`) and however the caller learned the id, including a run that engaged the full-catalog fallback earlier. A hidden skill has exactly two sanctioned routes: a sub agent that owns it (rungs 2–3), or the full-catalog fallback (§7.0), which runs it inline, in the open, with its own `route` step and chat banner. The planner prompt and the agentic skills catalog state the constraint — the catalog line for a non-exposed skill withholds the registry id `spin_worker` would need to quote — so the enforcement is announced, not just enforced.
  The ladder is deterministic and logged as a `route` step, so every run trace shows why a capability resolved the way it did.
- **Plan validation + repair**: the planner's JSON is schema-validated and every referenced id checked against the registries. Invalid output → one repair retry (same prompt + the validation errors). Still invalid → run fails with a clear chat message; the raw planner outputs are stored on the run for debugging.
- **Cancellation**: `POST /runs/{id}/cancel` is cooperative — the run task is cancelled at the next step boundary, in-flight steps marked `cancelled`, run status `cancelled`, checkpoint retained for inspection. Cancel on a `paused_hitl` run resolves it as cancelled.
- **SSE event contract** (`GET /chat/stream/{run_id}`), every event `{type, run_id, ts, payload}`:
  `plan {entries[]}` · `route {capability, rung, resolved_to}` · `dispatch_start|dispatch_end {step_id, tier, kind, entity_id, entity_name}` · `activity {step_id, parent_step_id, step_type, tier, kind, entity_name, node_id, status}` (every step start/finish — powers the chat's live activity ticker and the nested grouping of steps under their dispatch rails; payloads stay in traces) · `token {text}` (aggregator stream) · `thinking {text}` (streamed reasoning when the model exposes it — rendered as its own collapsible layout, never mixed into prose) · `answer_ui {a2ui: messages[], charts?, presentation, coverage}` (the formatter's structured artifact as A2UI v0.9 protocol messages plus its presentation mode and content-coverage percent, after the token stream, before `done`; emitted only when the formatter is enabled and produced a valid artifact) · `charts {charts: [..]}` (chart specs produced by the `render_chart` tool during the run — emitted whenever present, independent of the formatter) · `hitl_request {step_id, prompt}` · `run_status {status}` · `error {step_id?, message}` · `done {answer, tokens}`.
- **Dispatch**: entries with no unmet `depends_on` dispatch in parallel (`Send`); dependents wait on outputs. Each plan entry executes via its resolved capability — direct tool call, inline skill loop, registered native/custom sub agent, or factory-built ephemeral worker. Every step is recorded to `run_steps`.

### 7.2 Agentic mode

The orchestrator is a single `create_agent` with a finetuned system prompt (concierge instructions: when to answer directly, when to use capabilities, how to sequence) and the full middleware stack: TodoListMiddleware (visible planning/self-correction), SummarizationMiddleware, and the three registry middlewares — so its capabilities at every model call are exactly the live registries: exposed tools, exposed skills, sub agent dispatch tools, plus a `spin_worker(skill_ids, task)` tool for the rung-4 fallback. Properties relative to graph mode: planning is emergent (todos, not a validated plan artifact); parallelism only via parallel tool calls in one turn; the ladder becomes tool-construction policy inside the middlewares, with each capability invocation still logged as a `route`-equivalent step via the tool-call wrapper; HITL propagates through interrupt-in-tool with idempotent-replay dispatch. Same SSE contract (`plan` events carry the todo list), same run/step recording, same labels — traces from both modes are directly comparable, which is the point: the POC can A/B explicit-planner vs agentic orchestration on identical registries.
- **HITL propagation**: a worker interrupt pauses the run (`status=paused_hitl`), emits an SSE `hitl_request` event with the prompt **and any `questions` (§3.5 form gates)**; `POST /runs/{id}/hitl` resumes from checkpoint with `{decision, note?, answers?}` — `approve` continues, `deny` routes that node to END with a denial note in state, and `answers` (a `{question_id: value}` map) is recorded on the hitl step and delivered into worker state for downstream nodes.
- **Aggregate**: final LLM call merges worker outputs into one chat answer; token totals rolled up onto the run.
- **The formatter (declarative answer UI, A2UI, model-generated)**: an explicit presentation role, separate from the aggregator (which synthesizes *what to say*; the formatter decides *how to show it*). When `formatter_enabled`, a follow-up structured-output call (on `formatter_model`, null → `default_model`, through the §2.1 port — provider-agnostic by construction) **transforms** the canonical answer into a component tree with full content parity as the goal: every factual statement, number, warning, recommendation, and caveat preserved; only verbatim duplication dropped; prose carried in `text` (markdown) components so nothing is forced into structure. The transform is graded by a deterministic, code-computed **coverage** metric (retention of numbers/URLs/code spans from the raw answer) stored on the artifact — an instrument and visual flag (`formatter_coverage_flag_threshold`), never a render gate. When `formatter_enabled=false` the call never runs and no artifact exists for the run — the raw markdown answer renders directly (charts still render via the `render_chart` tool path). The model generates a compact component tree (whitelisted types: `card`, `text` (markdown), `stat`, `table`, `list`, `badge`, `divider`, `link`, `sources`, and — when `answer_ui_charts_enabled` — `chart {kind: 'bar'|'line'|'pie', title?, labels, series}`; chart data must be extracted from the answer/run content, never invented, and the prompt instructs the model to prefer a table unless the data is genuinely comparative or trending. Chart components are split out of the A2UI message stream into a `charts` array on the persisted payload and rendered by the app's own themed pure-SVG component — data only, no markup surface), which a **deterministic server-side translator converts into A2UI v0.9 protocol messages** (`createSurface` + `updateComponents`, literal values, basic catalog). The model contributes content; our code guarantees protocol-valid A2UI on the wire. Emitted as the `answer_ui` SSE event (`{a2ui: messages[]}`) and persisted on `runs.answer_ui`. **Failure-safe**: schema-invalid or failed generation is dropped silently — the streamed text answer is always the source of truth; the UI payload only ever augments it. Applies to both orchestrator modes.
- **Fallback**: the planner may return an empty plan with a direct answer for trivial requests needing no capability. When it instead reports no confident match for a capability-needing query, the self-service full-catalog fallback (§7.0) takes over rather than answering blind or force-spinning a worker.

### 7.3 Registry cache layer

All registry and settings reads — the three registry middlewares, the graph-mode planner catalog, the resolution ladder, the worker factory's id lookups, the settings store — go through one `RegistryCache` service exposing typed reads (`tools(exposed|full)`, `tools_by_ids`, `skill_snapshots`, `sub_agent_cards`, `sub_agent_snapshot`, `setting`, …). Its storage is a swappable backend selected by the live `registry_cache_mode` setting:

- **`bypass` (default)** — no state; every read executes the same Postgres queries as before the cache existed. Byte-identical semantics; the rollback lever.
- **`memory`** — per-process store loaded on startup, per-registry generation counters, **reload-on-dirty** (an invalidation marks the registry stale; the next read reloads it wholesale — registries are small, and full reload cannot leave stale embedded relationships).
- **`redis`** — same contract over Redis (read-through blobs, delete-on-invalidate), for future multi-replica deployments. `REDIS_URL` env-only; selecting the mode pings Redis and rejects the save if unreachable. Optional compose profile; excluded from the default test gate (tests skip without `REDIS_URL`).

**Cross-replica sync (ready, dormant on one node)**: every invalidation also fires `pg_notify` on a shared channel, and each process LISTENs on a dedicated connection, marking its local cache dirty (with relationship propagation) when a *peer* broadcasts — own notifications are origin-filtered, so loops are impossible by construction. Single-replica correctness never depends on the notify path (best-effort, failure logs). `quick-setup.sh` optionally provisions the Redis compose profile upfront (`--redis`/`--no-redis` or interactive prompt); actually using it remains a runtime Settings decision. pgvector remains a documented storage swap for when catalogs outgrow in-memory ranking — embeddings live in JSONB today so the stock Postgres image keeps working.

**Invalidation is event-driven and exhaustive** — every write path calls `invalidate(registry)` before returning: registry CRUD, `status`/`direct_exposure` toggles, MCP ingest and `listChanged` re-ingest, MCP server delete/disable cascades, seed reload, settings PATCH. TTLs are forbidden; a cache entry is either current or explicitly invalidated. Mode flips apply live (flipping into `memory` warm-loads; flipping to `bypass` is an instant escape hatch). `GET /cache/status` reports `{records, generation, loaded_at}` per registry; `POST /cache/refresh/{registry|all}` forces an eager reload (the UI refresh buttons, §8.7 — an operator override, not a correctness mechanism).

### 7.4 Progressive-disclosure retrieval (top-K)

The catalogs the orchestrator sees (§7.1 planner catalog; §7.2 exposed-mode middleware projections) are ranked and truncated to the most relevant records when a registry outgrows full injection. Off by default (`retrieval_enabled=false`); when enabled it activates **per registry only above `retrieval_threshold`** records — below that, full injection exactly as today, bit-for-bit.

- **Scoring** runs in-process over the cache snapshot (never a per-call DB query): lexical (BM25 over name + description) fused with vector cosine (reciprocal-rank fusion) when embeddings are available. The query text is the current task — plan entry text in graph mode, the latest user message/todo in agentic mode — with the query embedding memoized per task text.
- **Embeddings** are maintained on the write path (best-effort: failure logs and leaves the row unembedded, never fails the save) plus a startup backfill; stored per record (`embedding jsonb`, `embedding_hash`) on tools/skills/sub_agents via the §2.1 embeddings port. No embedding model configured → lexical-only scoring, silently.
- **Guarantees**: ids referenced by the current plan and entities already used in the run are pinned past ranking; full-catalog mode (§7.0 fallback, `use_full_catalog`) bypasses retrieval entirely, so a top-K miss is always recoverable; every truncation logs the drop count and the injected catalog carries a footer line ("showing N of M — use_full_catalog to widen") so the model knows it sees a slice. Skill loops, sub-agent workflows, and `spin_worker` are id-pinned contracts and are **never** subject to retrieval.

### 7.5 Direct sub-agent invocation

The user (or an API caller) can pin a specific sub agent to handle a request, replacing the **routing decision** — never the run lifecycle. Every direct invocation is a persisted run with `orchestrator_mode='direct'` and `target_sub_agent_id` set, executed by the shared runner: same SSE stream, same HITL pause/resume (direct runs ride the same checkpointer; a workflow's hitl nodes pause and resume identically), same run/step recording and §10 labels (`mode='direct'` in metrics), same failure finalization, and the **same formatter tail** — formatter on gives the sub agent's answer the full `answer_ui` treatment; formatter off renders it raw, exactly like any other run.

- **Surfaces** (all converge on one internal path — create run with mode `direct`, then execute):
  1. `POST /sub-agents/{id}/invoke` `{message, conversation_id?}` — programmatic; without a conversation id a fresh conversation is created, so the run is always chat-visible and auditable.
  2. `POST /chat` with `target_sub_agent_id` — conversational; the run lands in the conversation with history rendering as usual.
  3. Sub Agents page row action "Invoke →" — opens Chat pre-pinned to that agent (surface 2).
  4. Chat composer **target picker** — "Orchestrator (auto)" plus every active, exposed sub agent; picking one pins the next message (surface 2). **The pin is per-conversation state (M40)**: each conversation remembers its own target — and its history-summary checkbox — and switching conversations never carries a pin across. A new conversation always starts at Orchestrator (auto); reopening a conversation restores the pin it had; a `?target=` deep link (surface 3) pins only the conversation it opens.
- **Gating**: the sub agent must be `status='active'` **and** `direct_exposure=true` (§3.4) — enforced at the API surface (403 not exposed / 409 not active) and re-checked at execution start (defense in depth: a toggle flipped between request and execution fails the run cleanly). The flag closes all four surfaces at once.
- **Execution**: the direct branch resolves the pinned agent through the §7.1 ladder's sub-agent rungs (`native_sub_agent` via the registered graph, `custom_sub_agent` via the factory-compiled workflow — identical worker code paths to routed dispatch), records a `route` step with rung and target so traces stay comparable, and runs the worker with HITL propagation on the run's checkpointer thread.
- **Boundaries**: the routing ladder and planner are untouched — an orchestrator-routed run never carries `mode='direct'`. Sub agents still cannot invoke other sub agents; direct invocation is a user-initiated entry point, not an agent-to-agent surface. Conversation history is **not** injected into the pinned worker by default (workers are task-scoped, exactly as when dispatched by the planner); the conversation is the audit surface, not extra context.
- **Opt-in history summary** (`include_history_summary`, default false): the one sanctioned way to give a pinned worker conversational context, mirroring the role the planner plays for routed dispatch (it reads history and writes self-contained tasks — direct mode removed that translator). When the flag rides a pinned `POST /chat` (422 without a target) or a `POST /sub-agents/{id}/invoke` carrying `conversation_id` (422 without one), the direct branch makes ONE summarization call before the worker starts — default model at effort `low`, prompt in `app/prompts/history_summary.md`, over the same capped history window the planner sees — records it as a `summary` step (§3.6) with token usage rolled up, and hands the worker `summary block + the user's verbatim message`. The flag persists on the run (`runs.include_history_summary`), `retry` preserves it, and HITL resume never re-summarizes (the worker task is checkpointed). Flag off = byte-identical to the default cold behavior. UI (§8.5): a composer checkbox shown only while a sub agent is pinned AND the conversation has at least one completed run — never for the orchestrator (it always gets history), never on a conversation's first message; summarized direct turns carry a `+ctx` marker. **Opt-in memories** (`include_memories`, §16.3, default false): the same flag pattern for the L2/L1 memory block — 422 rules, run column, retry preservation, byte-identical when off; only meaningful while `memory_enabled` is on.


## 8. Admin UI

Single React app, left nav: **Chat, MCP Servers, Tools, Skills, Sub Agents, Runs, Settings**. Consistent table pattern everywhere: search, source filter, **kind filter**, `static`/`dynamic` badge, **kind badge** (`mcp`/`native`/`custom`), status pill, row click → detail/edit drawer. Static records: definition fields disabled + "static" notice, but status/exposure toggles remain live.

### 8.1 MCP Servers
- Table: name, transport, status pill (active/error + last_error tooltip), tool count, last connected.
- Register form: transport toggle → stdio fields (command, args, env key-values) or http fields (url, headers). **Env and header values render masked with reveal-on-click** — the POC stores them, the UI doesn't display them by default. Test-connection button (dry connect + tool count preview) before save.
- Row actions: reconnect, refresh tools, edit, soft delete (blocked with dependents dialog if its tools are bound to skills).

### 8.2 Tools
- Table: tool name, server, description, **skill badges** — one chip per skill that binds this tool, click chip → that skill's detail. Zero chips renders "unassigned" chip. **`direct` badge** when `direct_exposure=true`.
- Detail drawer: input schema (pretty JSON), server link, editable description/status, **"Expose to orchestrator" toggle**.
- Header: **Refresh cache** button + cache status line (`records · generation · loaded ago`, §7.3) — the same affordance appears on the Skills and Sub Agents pages for their registries.

### 8.3 Skills
- Table: name, persona preview (first line), **tool badges** (chips → tool detail), **sub agent badges** (chips per sub agent whose workflow uses this skill), source, status.
- Create/edit: **template-based skill document editor** — frontmatter as form fields (name, description, persona, "Expose to orchestrator" toggle, **optional model + effort override** (model select filtered to supported params, effort: none/low/medium/high, temperature), **tool tags**: searchable multi-select across the tool registry, grouped with system-seeded static tools first, source/kind badges on each) + markdown body editor pre-loaded with the skill template (Purpose / Steps / Output format sections); `{tool:...}` mentions autocomplete from tagged tools only and validate at save; side-by-side rendered preview. Save validates all tagged tool ids active + every mention resolves to a tagged tool. **Delete** (custom only) soft-deletes, blocked with a 409 dependents dialog if any active sub agent's workflow uses the skill. Save first runs the §4 overlap guard: at ≥70% match a dialog shows the match + judge reasoning with **Save anyway** / **Cancel (use the existing one)**.

### 8.4 Sub Agents
- Table: name, persona preview, model (or "default"), **skill badges**, run count, source, status.
- Create/edit: name, description, persona, **model + effort override selects** (model override select, effort none/low/medium/high, temperature — filtered to the model's supported params), **workflow builder** — form-based, opened from a **starter template** picker (Blank · Sequential pipeline · Branch + HITL approve · Parallel fan-out/join: pre-filled DAG skeletons with placeholder nodes the user fills via skill picker): node list (add skill node via skill picker / add HITL node with prompt), edge list (from → to + optional condition text + on: success/error), with a live read-only react-flow graph preview rendering nodes, edges, condition labels, and validation errors inline. Save runs `/validate`; compile errors shown next to the offending node/edge. **Delete** (custom only) soft-deletes the sub agent. Save also runs the §4 overlap guard: at ≥70% match a dialog shows the match + judge reasoning with **Save anyway** / **Cancel (use the existing one)**.
- **Native sub agents** render as a definition card — description, `covers_skill_ids` chips, `native_ref` — with no DAG builder (there is no workflow record; the graph is code). Read-only except status **and direct exposure**.
- **Direct exposure toggle** (§7.5): on every sub agent detail (custom editor and native card alike, live on static records) — controls whether the agent can be invoked directly. Exposed agents show a `direct` chip in the table.
- Row action: "Invoke →" — opens Chat **pre-pinned** to this sub agent via the target picker (§8.5); the next message runs as a `direct` run, planner bypassed. Shown only for active + exposed agents.

### 8.5 Chat
- Conversation sidebar (list + new conversation); selecting one loads its history. Streaming conversation (SSE): assistant tokens, plus inline system cards for plan (**graph mode**: sub agents + parallel groups; **agentic mode**: the live todo list, items checking off as the loop progresses), each dispatch start/finish, and **HITL cards** with the prompt + Approve/Deny buttons and optional note. **Self-service fallback engagement renders a distinct banner** ("full-catalog fallback — descriptions didn't route this") linking to its route step. Each response footer links to its run trace. While a run is working, a **live activity ticker** (from `activity` events) shows the step currently executing — the sub agent node, skill, or tool by name — one line, no payloads (those live in traces). Streamed `thinking` renders as a dimmed collapsible block above the answer. The **Send button becomes Stop** while a run is in flight — stop cancels the run (`POST /runs/{id}/cancel`) before it finishes or reaches HITL. **One message can be queued** while the agent works: pressing send mid-run queues the draft (editable in the composer until it fires) and it auto-sends when the current run finishes. **Target picker** (§7.5): a composer select — "Orchestrator (auto)" (default) plus every active, exposed sub agent — pins messages to that agent as `direct` runs; a visible chip shows the active pin, and user bubbles for direct runs carry a small `→ agent` marker so the history shows who handled what. **History-summary checkbox** (§7.5): beside the picker, shown only while a sub agent is pinned AND the conversation already has a completed run (never for the orchestrator, never on a first message); checked, the pinned message runs with `include_history_summary` and its bubble marker gains `+ctx`.
- **Answer rendering**: the canonical text answer renders as **markdown** (safe renderer, no raw HTML) — it is the primary, always-visible response.
- **Declarative answer UI renderer**: `answer_ui` payloads are A2UI v0.9 messages rendered with the official **`@a2ui/react` + `@a2ui/web_core`** renderer (`A2uiSurface` + `MessageProcessor`, basic catalog) — payloads are data, not markup (no HTML/JS injection surface). **Arrangement is artifact-driven and run-time-frozen**: a run renders by its own persisted artifact, never by current settings. Artifact present with `presentation: 'a2ui_first'` → the structured view is primary and the raw markdown collapses behind "view raw response"; `presentation: 'raw_first'` (and legacy artifacts without a presentation) → raw markdown primary with the artifact collapsed behind "show structured summary". No artifact (formatter off at run time, or its call failed — fail-open) → raw markdown renders directly and **no structured toggle exists at all**. `runs.charts` (the `render_chart` tool path) render with the primary content in every state. During live streaming the raw token stream is always the progress view; the run settles into its arrangement when the artifact arrives. The coverage percent shows as a quiet badge, amber below `formatter_coverage_flag_threshold`. Re-rendered from `runs.answer_ui` + `runs.charts` when a conversation reloads.
- **Form gates**: HITL cards carrying `questions` render them all on one card — choice chips, text inputs, approve toggles — with a single **Submit** (and Deny); a plain prompt keeps the classic Approve/Deny card. Answers appear in the run trace on the hitl step.

### 8.6 Runs
- Table: time, message excerpt, status, **orchestrator mode badge**, sub agents involved, duration, tokens.
- Detail: plan JSON, ordered step timeline grouped by sub agent (node id, type, model, tokens, duration, expandable input/output, tool calls with args/results), errors highlighted. Paused runs show pending HITL with resolve buttons. Row/detail actions: **cancel** (running), **retry** (failed — re-plans from the original message), **delete**.

### 8.7 Settings (command center)

- **Models**: default, planner, aggregator — each a `provider:model` select **plus params (effort none/low/medium/high, temperature, max output tokens), options filtered to what the selected model supports** — applied to next run, no restart. **Providers panel**: read-only list of registered provider adapters with configured/unconfigured status and their model lists (§2.1). **The rule (M43): every role model registered in §3.7 has a select plus params on this page**, in the section that owns the role — a role whose model can only be set by API is a hidden control, and a new role model is not shipped until its picker is. Today that means `default`/`planner`/`aggregator` here, `formatter` in the formatter block, `memory_extraction` in the memory block, and `ambient_salience` in the salience block. Provider API keys remain the one deliberate exception in the other direction: env-only, never in DB or UI (§13).
- **Orchestrator**: **mode toggle (graph | agentic, §7)**, **full-catalog fallback on/off**, **declarative answer UI on/off**, max parallel dispatch, max plan steps, dynamic-worker fallback on/off, direct-exposure cap warning threshold — when current exposures exceed the threshold, the Tools and Skills pages show a context-cost warning banner — plus (M40) the **overlap-guard threshold** (`overlap_threshold_percent`) and the **agentic recursion limit**.
- **MCP**: health-check interval; global reconnect-all and refresh-all-tools buttons.
- **Ambient (§17, M40)**: master `ambient_enabled` toggle (hint: the Ambient page appears in the nav when on), tick interval, run/routine/wakeup budgets, idle minutes, HITL timeout, digest times, quiet hours, notification/escalation budgets, interrupt threshold, learning mode, stall-reaper window (`run_stall_after_s`), channels routing, and (M41) the **pursuit select** (`ambient_pursuit`: off | away | always) sitting with the channel routing it modifies, hinted as "external channels fire only when the in-app toast reached nobody", plus (M42) the **salience block** — mode select (off | propose | auto), minimum-urgency prefilter, and an optional salience model override — hinted as "re-judges what an unseen alert actually said: lead the next digest, remember it, or drop it on the record" — the same live-PATCH pattern as every section; the master switch here mirrors exactly what the API accepts.
- **A2A (§19, M40)**: master `a2a_enabled` toggle (hint: the Remote Agents page appears in the nav when on), card refresh interval, task timeout, poll interval, max parked (0 disables parking), HTTP timeout, fence cap.
- **API guardrails (M40)**: rate-limit burst + refill per second (`rate_limit_burst` / `rate_limit_per_s`) — admin-gated like every settings write.
- **Registry cache (§7.3)**: mode select (`bypass` | `memory` | `redis` — redis offered only when `REDIS_URL` is set, save pings it), per-registry status readout (records, generation, loaded-at), **Refresh all caches** button.
- **Retrieval (§7.4)**: enabled toggle, threshold, top-K, embedding model (`provider:model`, validated at save; blank = lexical-only).
- **Observability**: log level select, LangSmith toggle, OTLP endpoint field.
- **HITL queue**: all currently paused runs across chats, resolvable inline.
- **Appearance**: theme picker — `default` (mission-control, ships as default), `anthropic`, `openai`, `google`; stored client-side (localStorage), applied instantly, no backend setting.
- **Data**: seed-reload button (idempotent), run-history purge with confirm.
- Every control maps to `app_settings` or an existing endpoint — nothing on this page requires a container restart; API key is deliberately absent (env-only).

### 8.8 Memory (§16)

- Store browser: searchable/filterable list (scope, kind, status, source) over `memories`; edit-as-supersede (`source='user_edited'`), pin/unpin, hard delete — which with `memory_forget_enabled` on becomes two verbs, **Forget** (primary; tombstoned, re-admission suppressed) and **Erase** (explicit; physical, no trace) (§16.1, M44); provenance links into run traces; bi-temporal fields visible on the row detail.
- **Forgotten section (M44)**: tombstones listed metadata-only (kind, scope, source, forgotten-at, times-suppressed — there is no text to show), each with a working **Unforget**. Suppression must never look like "memory mysteriously won't learn X" — the mechanism is on the page, and the escape hatch beside it. Renders only when `memory_forget_enabled` is on.
- Review queue: quarantined rows (extracted/inferred `instruction` memories, ambiguous contradictions) approved/rejected inline with an optional note — the HITL card pattern.
- Layer status: per-table counters, last-consolidation timestamps per job, and the eval axes (injected tokens, recall latency, store growth).
- Every control maps to the §16 API; the page renders only when `memory_enabled` is on.

### 8.9 Ambient (§17)

Four tabs: **Routines** (CRUD, trigger editor with the filter-operator set,
fire-token lifecycle, per-routine run history + auto-pause reasons),
**Watches** (standing intents: original text + compiled rule echo, watermark,
cadence/backoff state, expiry), **Inbox** (Notify · Question · Review items,
digest preview, approval batch ranked by risk), **Ledger** (fire/hold audit
with reasons, correlation-chain view for patterns, intervention-precision
sparkline per category). Chat composer gains nothing — ambient never changes
the interactive surface when dark. The page renders only when `ambient_enabled` is on.

**Salience on the delivery card (M43).** A §17.5 verdict is shown where the
delivery already lives, not in a separate queue — a second inbox to triage
would cost more attention than the feature saves. The card leads with the
*consequence* in plain language ("Worth your attention" / "Worth
remembering" / "Looks like noise"), never the mechanism. A proposed verdict
offers **Do it** and **Leave it**; an applied one states what happened and
offers **Undo** while undo is still possible, or says why it no longer is.
Nothing is hidden: an expandable **why this?** carries the judge's reason,
confidence, the mode that produced it, and the fact that a model made the
call — demoted below the plain-language line, never omitted.

### 8.10 Remote Agents (§19)

Register an external A2A agent by card URL; agent table (status, skill
count, projected tools, last card refresh); detail drawer with the fetched
Agent Card, its declared skills, per-scheme credential status (configured
or not — values are write-only and never displayed) and a masked
credential form supporting `env:VAR` indirection, refresh-card action, and
delete (409 while skills bind its projected tools); a tasks view listing
open/parked/recent remote tasks with reply (for `input-required`) and
cancel. Every control maps to the §19 API. The page renders only when
`a2a_enabled` is on.

## 9. Seed Data (static)

Loaded idempotently at startup, all `source=static`:

1. **MCP servers**: `fetch` (`uvx mcp-server-fetch`, stdio) and `filesystem` (`npx -y @modelcontextprotocol/server-filesystem /workspace`, stdio, sandboxed to a `/workspace` volume).
2. **Skills** (native `.skill.md` files in `app/native/skills/`): `web-research` (persona: careful researcher, cite sources; tools: fetch; instructions: multi-step research process where the final synthesis step ties no tool), `file-ops` (persona: precise file clerk, confirm paths; tools: read/write/list from filesystem server), `workspace-auditor` (read-only workspace mapping; tools: directory_tree / list_directory_with_sizes / search_files / get_file_info — filesystem tools no other skill binds) and `workspace-curator` (tidy-never-delete reorganizer; tools: create_directory / move_file / read_multiple_files). The auditor/curator pair proves skills composed purely from otherwise-untagged tools.
3. **Sub agents**: `research-concierge` — persona: helpful research concierge; workflow: `START → research(web-research) →[found results]→ hitl("Save findings to a file?") → write(file-ops) → END`, with `research →[nothing found]→ END` branch. Exercises DAG branching + HITL + both skills in one seed. And `workspace-warden` — a **native** sub agent (§3.4): a code-defined two-stage graph (`audit → curate`) over `workspace-auditor` + `workspace-curator`, registered via `@native_sub_agent` with the covered skills named in code and resolved to registry uuids at seed time; each stage delegates to the factory's skill-node semantics, so scoped tools/model resolution/middleware behave exactly as in factory-built workers. Ships `direct_exposure=true`, so the native tier is exercisable through every §7.5 surface out of the box.
4. **Native tool**: `summarize-and-structure` (§5b), bound into `web-research` — proves mixed mcp+native tools in one skill.
5. **Declarative agent file**: `workspace-reporter.agent.md` (§3.4) — `START → audit(workspace-auditor) →[findings]→ hitl(form gate: report format) → write(file-ops) → END`, with an `[empty workspace]→ END` branch. Proves the `.agent.md` path end to end: skill-by-name resolution across both skill generations, a form gate, branching, and §7.5 direct exposure — all from one file.

## 10. Observability

Every span and log line carries the label set: `{run_id, step_id, tier ('tool'|'skill'|'sub_agent'|'orchestrator'), kind, source, entity_id, entity_name, model, effort, input_tokens, output_tokens, duration_ms, status}`. Sub-agent-tier `kind` values: `native | custom | dynamic` (ephemeral workers).

- **Structured logs**: structlog, JSON to stdout — directly shippable to ELK/Filebeat without parsing config. One log event per registry mutation, MCP lifecycle event, run step start/finish, and error.
- **Traces**: OpenTelemetry instrumentation over the orchestrator graph, worker invocations, tool calls (nested spans mirror `run_steps.parent_step_id`). OTLP exporter, endpoint via env — points at Grafana Tempo/Jaeger/anything OTLP.
- **LangSmith**: full framework-level traces (prompts, completions, tool calls) for every LLM touchpoint — both orchestrator modes, all `create_agent` loops, dispatched sub agent graphs, fallback loops. **Local or remote by endpoint**: `langsmith_endpoint` (self-hosted instance or SaaS) + `langsmith_project` + `langsmith_enabled` live in `app_settings`; the tracer is constructed **per run** from settings and injected via callbacks — not process env — so enabling/pointing LangSmith requires no restart. `LANGSMITH_API_KEY` alone stays env-only. Runs simultaneously with OTel; `run_id` is attached as LangSmith metadata so PG traces, OTel spans, and LangSmith runs cross-reference. This is the connectivity the §15 eval feature reuses (datasets + experiment results via the same client config).
- **Metrics**: Prometheus `/metrics` — counters (runs, steps, tool calls, errors) and histograms (run duration, step duration, tokens), all labeled with `tier/kind/source` — Grafana dashboards slice native vs custom vs mcp directly.
- **Correlation**: `run_id` present in logs, spans, and metrics exemplars; UI run trace links are the same id.

## 11. Testing

- **Backend unit** (pytest): skill document parsing (frontmatter + body, `.skill.md` startup scan, `{tool:...}` mention validation incl. mention of an untagged tool rejected), worker factory (DAG → graph compile: sequential, branch, parallel, error edges, reachable-join semantics, HITL, multi-skill ephemeral build, invalid DAG rejection matrix), persona merge order, planner validation + repair loop, registry validation rules (referential integrity, static write rejection), MCP manager against a stub MCP server (connect, ingest, listChanged reconcile, error status), native tool provider (registration scan, schema derivation, HITL-in-subgraph rejection, sub-agent-wrap rejection, mixed mcp+native skill invocation, nested trace + token rollup).
- **API contract** (pytest + httpx): CRUD per registry, cross-reference endpoints, 403 static, 409 dependents, chat → run → HITL resume happy path with mocked LLM, capability resolution ladder (direct tool, direct skill, native match via covers_skill_ids, custom match, ephemeral dynamic worker fallback — one test per rung, plus precedence when multiple rungs match), registry middlewares (live tool appearance mid-loop, skill/sub-agent tool construction from registry state, idempotent dispatch replay, **strict tool isolation — a skill loop can never see unbound tools, including in fallback**, full-catalog fallback triggering on no-confident-match), both orchestrator modes over the same fixture registries.
- **UI** (Playwright): register stdio MCP server → tools appear → create skill → badges correct on both Tools and Skills pages → build sub agent with branch + HITL, validation error then fix → chat invoke → approve HITL card → run trace shows all steps. Static record toggle-only rendering. Settings page: model change reflected in next run's trace labels, run cancel/retry.
- **Registry cache (§7.3)**: a backend contract suite runs identically against `bypass` and `memory` (typed reads, invalidation-after-write ordering, refresh, status); the middleware/ladder/orchestrator suites are parametrized over both modes, including the live-sync case (expose a tool mid-run → next model call sees it). Redis backend tests are env-gated (`REDIS_URL`) and excluded from the default gate.
- **Retrieval (§7.4)**: ranker unit tests (lexical, vector via fake embeddings, RRF fusion, threshold gate, pinned ids, drop counting); embeddings port covered by the §2.1 adapter contract suite; integration test that an over-threshold catalog truncates with footer while pinned ids survive.
- LLM calls mocked/recorded in all tests via a LangChain fake chat model injected through the §2.1 factory — tests never touch a provider SDK. One optional live smoke test behind an env flag, parameterized over every provider with a configured key (proves provider-agnosticism on real APIs).

## 12. Milestones

| # | Deliverable | Proves |
|---|---|---|
| M1 | Postgres schema + registry API + seed + static rejection rules | Registries as single source of truth |
| M2 | MCP manager: stdio + http connect, ingest, listChanged, health | Dynamic tool tier |
| M3 | Worker factory + validate endpoint (sequential, branch, parallel, HITL compile) | Dynamic sub agent tier |
| M4 | Orchestrator both modes (graph + agentic) + middleware layer (§7.0) + chat SSE + HITL resume + run recording + observability (structlog JSON, OTel spans, /metrics) | End-to-end invocation, traceable, mode-comparable |
| M5 | Admin UI: all seven pages incl. Settings command center, badges, workflow builder, trace view, run controls | Full admin manageability — every runtime control in UI |
| M6 | Test suites green + docker-compose polish + README | Done |
| M7 | Registry cache layer (§7.3: bypass/memory/redis backends, event invalidation, refresh UI) + progressive-disclosure retrieval (§7.4: hybrid top-K, embeddings port, dark by default) | Production-shape read path without degrading POC behavior |

| M13 | Memory substrate (§16.1/16.4/§3.7): pgvector image pin + extension migration, memory tables incl. `memory_embeddings` side-table, memory service (CRUD, supersession, hybrid RRF + composite scoring, admission gate), `memory.recall/remember/forget` native tools + `memory-keeper` skill (seeded, hidden), settings, purge extension | Memory as data + registry citizenship, dark |
| M14 | Episodic layer (§16.2/16.3): post-run digests + conversation rollups, consolidation scheduler skeleton (advisory locks, debounce, idle detector), budgeted injection blocks (planner/agentic), `include_memories` for direct runs | Cross-conversation recall without behavior change when off |
| M15 | Semantic layer (§16.2): extraction → admission gate → LLM-match/code-resolve reconciliation, bi-temporal supersession live, instruction quarantine, Memory UI page (§8.8) incl. review queue, abstention + score floor | Durable facts with governed writes |
| M16 | Procedural learning (§16.5): routing stats, plan exemplars with vote lifecycle, planner few-shot block, fallback mining → `.skill.md` proposals through doclint + overlap judge | Measured drop in stage-30 fallback rate |
| M17 | Consolidation + evals (§16.2/16.9): decay sweep, reflection, contradiction sweep, memory probe suite + ablation experiment harness (per-layer configs vs memory-off baseline, measuring accuracy/tokens/latency) | The memory experiment: which layers earn their keep |
| M18 | Closed-loop refinement (§16.7): citation feedback (cited-vs-injected reinforcement), digest compaction (period digests, bounded episodic store), entity-hop recall + multi-hop probe, long-horizon time-warp simulation | Sharpen the winning layers with measured feedback |
| M19 | OpenRouter gateway adapter (§2.1 custom-gateway scenario) + cross-provider retest matrix | Provider portability, measured |
| M20 | Ambient substrate: all tables + migration, settings, webhook endpoint + token lifecycle, NOTIFY-wake drain, **real idle detector + presence states**, run `trigger`/`last_heartbeat_at`, byte-identity when dark | §14c-27; curl-able fire |
| M21 | Trigger + decision planes: schedules with stagger, adaptive pollers, state conditions, internal events, three-tier gate + fire/hold ledger, caps/drops/auto-pause, **CEP-lite (sequence/conjunction/absence) with all four chaining guards** | scripted-event harness: tier precision, absence-timer slop, cascade guards |
| M22 | Execution plane: routines end-to-end (both orchestrators, narrowed projection, budgets, abstain, progress monitor), standing intents (compile-echo-confirm → evaluate → fire), **agent wakeups with clamps/caps/done-guard**, watchdog + orphan rescue, HITL timeout semantics | §14c-20..25 |
| M23 | Delivery plane + §8.9 UI: outbox tiers, digest builder + return-flush + supersede-collapse, budgets/quiet hours, approval batching, **feedback capture + reward computation substrate** (blended reward persisted per delivery), rule-based precision auto-downgrade; anticipation job with hit-rate metric | §14c-26 + stage-3x UI evidence campaign |
| M24 | Ambient evals: simulated-clock event harness (fire/hold Set-F1 + false alarms, reaction time, cost) across gate ablations and delivery policies incl. cascade stress; multi-day live soak; experiment report | 07-style results doc |
| M25 | Adaptive policy learning (§17.7): bandit learner over the M23 reward substrate — digest-time shifting + category auto-tiering, **auto mode first-class (no approval), propose mode optional**, clamps, ledgered revertible changes; measured against static policy on the M24 harness | learning-on ≥ static policy on intervention precision; zero learner-caused tier-0 escalations |
| M26 | Ambient completeness pack (§18.1): per-routine `model_ref` honored at execution; near-due poller tightening; escalation-budget enforcement on digest approvals; per-item anticipation deliveries; learner threshold recovery; multi-time digest shifting; judge token accounting via `usage_metadata` | every §17 sentence has running code behind it |
| M27 | Memory context pack (§18.2): routine `include_memories` + persistent per-routine conversation (cross-fire continuity); `project` memory scope end-to-end; remembered-context block in the graph aggregator | a routine's second fire demonstrably knows its first |
| M28 | Real trigger sources (§18.3): parameterized poll-source contract; native `http_json` / `rss` / `mcp_tool` sources + native state probes, boot-registered; watch compiler lists live sources | §14c-29 |
| M29 | Delivery channels (§18.4): channel adapter registry — email (SMTP, env-configured) + generic webhook push (SMS-gateway-shaped); per-tier channel routing setting; per-channel send ledger; global ambient SSE stream + in-app tier-0/1 toast | §14c-30: a digest lands in a real inbox |
| M30 | Ambient UI completeness (§18.5): typed trigger/filter builder, ledger correlation-chain view + precision sparklines, per-routine run history, watch authoring (compile-echo-confirm from the page) | stage-4x UI evidence campaign |
| M31 | Memory communities (§18.6): label-propagation communities over entity links as a consolidation job; generative community summaries; community-aware recall breadth | multi-entity probe answered via a community summary |
| M32 | Evals feature (§15, promoted to in-scope): dataset upload (csv/xlsx), batch runner over skills + sub agents with `eval=true` labels and HITL auto-approve, graders (exact/contains/LLM-judge), results UI, LangSmith publish when configured | §14c-31 |
| M33 | Custom gateway adapter (§18.7): `custom` provider — OpenAI-compatible, env base-url/key, model list from a validated setting; proves the §2.1 seam with zero consumer changes | adapter contract suite green + live call through the port |
| M34 | Auth & tenancy (§18.8, dark by default): `auth_enabled` master switch; users + scrypt + bearer sessions + `admin\|member` roles; per-user scoping of conversations/runs/routines/watches/deliveries/presence; login UI; bootstrap admin; basic rate limiting | byte-identity with auth off; two users can't see each other's work |
| M35 | Multi-replica coordination (§18.9): advisory-lock leader election for the ambient tick with lease + failover; drain/executor already SKIP-LOCKED-safe | two concurrent loops: exactly one ticks; takeover on leader stop |
| M36 | Full acceptance ceremony: fresh volumes, fresh `docker compose up`, the ten-step script + §14c-20..31 top to bottom + channel/eval/gateway/auth stages; final report | the definition of done, re-earned end-to-end |
| M37 | A2A substrate (§19.1–19.4): `remote_agents` registry + card fetch/refresh, `a2a-sdk` isolated in `app/a2a/`, credential store (masked write-only, `env:` indirection) + scheme dispatch (apiKey/basic/bearer/oauth2 client_credentials), per-card-skill tools projection `kind='a2a'`, Remote Agents UI page, scripted in-process A2A counterparty + contract tests | byte-identity with a2a off; §14d-33..35 |
| M38 | A2A execution (§19.5): lazy call-time proxy via `materialize_tool`, streaming+polling consumption, all nine task states mapped, `input-required` ⇄ HITL gate with replay-idempotent task adoption, untrusted-fenced outputs, Stop → `tasks/cancel`, `a2a` step labels (+ direct-tool kind-label fix) | §14d-36..38 |
| M39 | A2A long-running (§19.6): park-on-budget, ambient leader-tick poller → outbox deliveries, task drawer reply/cancel, ExComm demo composition | §14d-39..40 |
| M48 | The switchability rule made true (§3.7.1, from an independent pre-public audit of every gate's enforcement site): six behaviors the system performed on its own with no switch of their own get one — the four consolidation jobs (`memory_decay_enabled`, `memory_contradiction_enabled`, `memory_communities_enabled`, `memory_compaction_enabled` — the last hard-deletes, so it mattered most), the anticipation job (`ambient_anticipation_enabled` — the only feature that initiates contact unprompted), and the §15 eval surface (`evals_enabled`). Every default equals the behavior it replaces, so the promotion is byte-identical. Plus the two §3.7.1 corollary fixes: `memory_community_budget_tokens = 0` now skips the rebuild instead of silencing injection while the job kept spending tokens, and the Settings page labels the legal-but-degraded `memory_forget_enabled`-without-`embedding_model` case. Settings coverage closed to 89/89 (`memory_digest_compact_days`, `memory_community_budget_tokens` were validated and unreachable) and is now **asserted by test**, so a future key with no control fails the suite. Dead `STALL_AFTER_S` removed | §14k-61..63; byte-identity at defaults; coverage assertion is the regression guard |
| M47 | Extraction tuner (§17.7 second consumer — the learner the M44 no-consumer note reserved): deterministic rules over machine-write tombstones (confidence-at-admission metadata) + quarantine rejections, MACHINE_SOURCES only; **kind routing** into `memory_quarantine_kinds` (≥ 50% repudiated over ≥ 5; per-kind chips in Settings clear it) and **band-local admission-floor moves** (±0.05, [0.5, 0.9], `setting:` proposals through `_apply_special`; the floor rises only when the band a bump would refuse is itself ≥ 60% repudiated — the raw-rate trigger ratchets, a harness finding); own gate `memory_extraction_learning` off\|propose\|auto default off. Evidence on the two-world harness: world A (kind-concentrated junk) learner 10 junk admissions at zero valuable-blocked vs 42–60 for every zero-loss static — no static floor separates entity junk (≤ .75) from preference (≥ .72); world B (cross-kind low-confidence junk) the floor walks 0.5→0.55→0.60 and stops at the zero-collateral point, beating the shipped default 36 vs 66 but NOT the retrospective oracle static (12) — a single-dial learner converges to its dial's oracle, it cannot beat it in-window; reported as counter-evidence, not tuned away | §14j-59..60; 17 contract tests; born dark — no live consumer until a human flips the gate |
| M46 | Embedding backfill job (§16.2 — the promised scheduler job, built): the `MemoryEmbedding` side-table contract ("a model switch re-embeds in the background and flips") gains its worker — an advisory-locked hourly job that embeds every live row lacking a vector under the ACTIVE model key, across all three `_embed_ref` surfaces (memories active+quarantined, run digests, active plan exemplars), batched, pass-bounded, old-key rows coexisting untouched. Also repairs write-through failures. Tombstones deliberately excluded: they keep no text, so pre-switch tombstones degrade to hash+anchor matching permanently — privacy over recall, by explicit design | §14j-58; closes the stage-32 known gap; 10 contract tests |
| M45 | Salience tuner (§17.7 — the feedback-consumer rule exercised): deterministic learner over the M43b `judge_reward` ledger — per-category mutes (`salience:<cat>` rows; revert un-mutes, M44 reject stays inert) and clamped urgency-floor moves (±1, [2,5]) through the existing proposal queue; own gate `ambient_salience_learning` (off\|propose\|auto, default off — byte-identical dark); Settings select beside the salience block. Evidence-first on the M24-pattern harness (`experiments/feedback_loop/`): learner precision .655 vs best-static .622 and default-static .483, at zero missed-critical and zero clamp violations; the forget-gate sweep separately confirms the M44 anchor mechanism is on-frontier while the exact threshold is population-sensitive — the learner's future knob, not a hardcode | harness report in `docs/research/feedback_loop/report.md`; 13 contract tests; no live consumer until the gate is flipped by a human |
| M44 | Durable forgetting + feedback-trace completeness (§16.1/§16.2/§17.7/§8.8): user deletion splits into **Forget** (metadata+hash tombstone, embedding copy for suppression only; the §16.2 admission gate suppresses re-admission of forgotten facts — exact hash, cosine ≥ `memory_forget_similarity`, or gray-band cosine ≥ 0.70 with a shared payload-token anchor (the hybrid gate, calibrated live) — so deletion finally keeps its promise; user re-assertion overrides and unforgets) and **Erase** (physical, no trace — privacy by explicit choice; purge clears tombstones too); Memory page gains the Forgotten section with working Unforget; §17.7 pending proposals gain explicit reject (capture-only); overlap-guard overrides logged content-free. `memory_forget_enabled` default false | byte-identity at defaults; §14i-55..57; tombstones have no consumer — a future learner enters under the §17.7 rule |
| M43 | Salience decision surface + settings completeness (§17.5/§8.9/§8.7): the `propose` mode M42 promised becomes real — a verdict renders on its own delivery card with **Do it** / **Leave it**, plain-language consequence headlines and layered "why this?" disclosure; every applied verdict (human- or auto-) gains a working **Undo** that restores the snapshotted state and retracts only the memories retention created, refusing honestly once a digest has spent the escalation; apply and decline write the judge's own reward onto the salience record — kept separate from the delivery's §17.7 feedback so judging the judge never pollutes §17.3 category precision — making the judge evaluable; decisions are idempotent, conflict-refusing and tenant-scoped. Plus the §8.7 rule that every §3.7 role model has a Settings picker, closing the two that were validated but unreachable (`memory_extraction_model`, `ambient_salience_model` — the latter promised in the M42 §8.7 text and not built) | §14h-52..54; no state change in `propose` until a human acts; undo restores byte-exactly |
| M42 | Delivery salience + truthful delivery record (§17.5/§18.4/§16): `in_app` becomes a first-class ledger entry written only when the in-app broadcast reached nobody (real-time modes only — a digest reaching an empty room is normal), `seen_at` + an unread nav badge make attention a fact rather than an inference, and the **salience pass** re-judges the CONTENT of unseen tier ≤1 deliveries — deterministic prefilter (urgency, category, `skey` recurrence, previously collapsed and never read as a signal) then a fail-open LLM judge over the fenced body — into three ledgered outcomes: escalate to digest-lead (never re-interrupt), retain into §16 with delivery provenance, or drop on the record. `ambient_salience_mode` default `off` | byte-identity at defaults; §14g-48..51, incl. a randomized regression sample proving unchanged surfaces |
| M41 | Ambient pursuit (§17.5/§18.4): `ambient_pursuit` ('off'\|'away'\|'always', default 'always' = pre-M41 behavior) gates the external half of `dispatch_delivered` on whether the in-app half reached anyone; the presence oracle is the SSE subscriber set sampled at dispatch — the literal audience of the toast just sent, correct per-process under §18.9 — never the idle timer; strictly subordinate to quiet hours, tiers, and the notification budget; Settings control beside the channel routing it modifies | tri-state matrix green + §14f-45..47 live against local SMTP and SMS-gateway-shaped webhook sinks |
| M40 | Config hardening + per-chat target pin: the §7.5 composer pin (and history-summary checkbox) become per-conversation state; `a2a_poll_interval_s` wired (tick-bounded watermark); Ambient + A2A + API-guardrail sections on the Settings page; hardcoded constants promoted to live settings (`ambient_tick_interval_s`, `rate_limit_burst`/`rate_limit_per_s`, `overlap_threshold_percent`, `run_stall_after_s`, `agentic_recursion_limit`, `a2a_http_timeout_s`, `a2a_fence_max_chars`) each with validation, defaults equal to the previous constants; auth session TTL moves to env (`AUTH_SESSION_TTL_H`) | byte-identity at defaults; §14e-41..44 |

Each milestone lands with its tests. M1–M4 are API-verifiable via curl before M5 exists.

## 13. Environment & Conventions

Env vars (all in `.env.example`, committed — no secrets in it): `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` (all optional, never in DB/UI — presence enables that provider in Settings model selects, spec §2.1; at least one key, or `FAKE_LLM_ENABLED=1`, is needed for runs to execute. If the code default's provider has no key at first boot, the seed pass stores the first configured provider's flagship as `default_model` — preference order `anthropic:claude-sonnet-4-6` → `google_genai:gemini-3.6-flash` → `openai:gpt-5.6-luna` → `fake:scripted`; an explicitly saved setting is never touched), `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` (compose db init), `DATABASE_URL`, `LANGSMITH_API_KEY` (key only — enable/endpoint/project are runtime settings, §10), `OTEL_EXPORTER_OTLP_ENDPOINT` (bootstrap default; the `otlp_endpoint` setting overrides at runtime), `WORKSPACE_DIR` (filesystem MCP sandbox), `REDIS_URL` (optional — enables the `redis` registry-cache mode, §7.3; URL-with-credentials stays env-only like every secret), `BACKEND_PORT`, `FRONTEND_PORT`, `VITE_API_BASE_URL` (frontend → backend, build-time), plus the §18 vars: `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`/`SMTP_TO` (email channel, §18.4 — credentials env-only), `AMBIENT_WEBHOOK_URL` (webhook push channel, §18.4), `CUSTOM_GATEWAY_BASE_URL`/`CUSTOM_GATEWAY_API_KEY`/`CUSTOM_GATEWAY_MODELS` (comma-separated model ids — the custom adapter's model list is env-configured to keep the sync `list_models()` port contract, §18.7), `AUTH_ENABLED` (§18.8, default false), `AUTH_SESSION_TTL_H` (§18.8, default 24 — bearer-session lifetime in hours; env like the auth master switch, M40).

Conventions: Python — ruff (lint+format), mypy strict on `app/`, pytest, async SQLAlchemy, Pydantic v2 schemas separate from ORM models. TypeScript — eslint + prettier, strict tsconfig, TanStack Query for API state, no Redux. Conventional commits. Alembic migration per schema change. All LLM prompts live in `backend/app/prompts/` as versioned files, not inline strings.

**Seed-document lint** (`python -m app.doclint`, `app/doclint.py`): the `.skill.md` (§3.3) and `.agent.md` (§3.4) formats are validated *offline* — no DB, no keys, no network — by the same rules the seed applies at boot: document parsing, `{tool:...}` mentions resolving to bound tools, tool-key hygiene, duplicate names across files, `model` as a `provider:model` reference, `model_params` against the live `ModelParams` contract (§2.1, so a typo'd key or effort value fails here), by-name skill references resolving to scanned skill files, and the full §3.5 structural DAG + form-gate validation. Errors fail; warnings flag legal-but-questionable documents (filename/name mismatch, empty description or persona, unknown frontmatter keys, uuid skill references that can only be checked at seed time). It runs three places: as a **Docker build gate** (`RUN python -m app.doclint` — a malformed document fails the image build instead of surfacing at boot as a missing skill or an agent stuck at `status='error'`), as a **pytest regression gate** over the documents this repo ships, and by hand during authoring. Seed-time validation is unchanged and remains authoritative for everything only the registry can know (skill status, dynamic records).

## 14. Acceptance Demo Script (definition of done)

Runs top to bottom on a fresh `docker compose up`, no restarts:

1. Open admin → seed visible: 2 MCP servers, tools with `static` badges, 2 skills, `research-concierge`, 1 native tool.
2. Register a new stdio MCP server from the UI → its tools appear with `{server}.{tool}` keys.
3. Create custom skill `summarize-site` from those tools + persona; badges appear on both Tools and Skills pages.
4. Toggle `direct_exposure` on one tool; verify next chat's trace shows a rung-1 `route` step.
5. Build custom sub agent with a branch, an error edge, and an HITL node; introduce a validation error, see it rejected inline, fix, save.
6. Chat: multi-turn — message 1 invokes the new sub agent (approve the HITL card mid-run); message 2 is a follow-up referencing message 1's result and succeeds using conversation history.
7. Ask something no capability covers → the planner reports no confident match and the trace shows the `fallback` route rung (full-catalog fallback, §7.0/§7.2 — unexposed skills are invisible to the planner by design, so it never force-spins a worker). The rung-4 ephemeral `dynamic` worker remains reachable when a plan or the agentic loop names specific skills via `spin_worker(skill_ids)` and is covered per-rung by the API test suite.
8. Kill the new MCP server process → invoke again → error edge path taken, run completes via fallback branch; server shows `error` status; reconnect from Settings.
9. Runs page: full trace with nested native-tool steps, tokens, route reasons; cancel a running run; retry a failed one.
10. Settings: change planner model → next run's trace labels show it; if a second provider key is configured, switch `default_model` to that provider (e.g. `google_genai:gemini-2.5-pro`) and rerun step 6's chat successfully — same registries, same code; open HITL queue; purge run history.
11. Switch `orchestrator_mode` to `agentic`, repeat step 6's first message: todo events stream, the same sub agent is reachable as a dispatch tool, HITL card still pauses/resumes, and the run trace remains label-comparable to the graph-mode run. While the agentic loop is mid-conversation, plug one more MCP tool and confirm it's callable within the same session (middleware live-sync).

All eleven pass = POC proven.

**§14b Memory acceptance additions (M13–M17; run with `memory_enabled=true` unless stated):**

12. **Regression**: steps 1–11 top to bottom with `memory_enabled=false` on a memory-capable build — byte-identical behavior, no memory steps in any trace.
13. **Tool surface (M13)**: expose the `memory-keeper` skill, ask the concierge to remember a fact, recall it in a new conversation via `memory.recall`, forget it, and verify the Memory page reflects each transition (active → expired).
14. **Cross-conversation recall (M14)**: state a project fact in conversation A; in fresh conversation B ask a question needing it — the planner context shows the budgeted memory block, the answer uses the fact, and the trace records injected token counts.
15. **Supersession + temporal (M15)**: state a fact, later correct it; ask "what is it now" (new value) and "what was it before" (old value from validity intervals); verify the superseded row is closed, not deleted, and the chain renders in the UI.
16. **Quarantine (M15)**: cause extraction to produce an instruction-kind memory — it lands quarantined, does NOT change behavior, appears in the review queue; approve it and see it apply on the next run.
17. **Abstention (M15)**: ask about something memory does not cover — no memory block injected below the score floor, and the answer does not fabricate a remembered fact.
18. **Procedural (M16)**: after several successful runs, verify routing stats on registry pages and a plan-exemplar few-shot in the planner prompt trace; re-run the stage-30 prompt suite and report the fallback-rate delta.
19. **Experiment matrix (M17)**: run the probe suite across layer configs (off / L1 / L1+L2 / full) and produce the comparison table (accuracy per ability, injected tokens, latency).

**§14c Ambient acceptance additions (M20–M25; run with `ambient_enabled=true` unless stated):**

20. Create a scheduled routine in the UI; observe the staggered fire, the
    run with trigger provenance, and the digest delivery.
21. Fire a routine via `curl` with its bearer token and an adversarial
    payload ("ignore your instructions…"); verify the run starts and the
    payload is fenced and not obeyed.
22. Create a standing intent conversationally; verify the compiled-rule
    echo, then plant a matching event and a non-matching event; exactly one
    fire.
23. Absence pattern: arm "if X doesn't arrive by T"; verify the timer fires
    within one tick of T.
24. Agent wakeup: a routine run schedules its own re-check; verify clamp,
    the done-guard, and cancellation.
25. Kill a watcher mid-poll; verify the watchdog stalls→pauses it with a
    visible reason and the orphaned job is rescued.
26. Urgency 5 event during quiet hours vs outside; verify budget debit,
    quiet-hour suppression to digest-lead.
27. `ambient_enabled=false`: byte-identity regression suite passes.
28. (M25) With `ambient_learning_mode='auto'`, dismiss a category three
    times; verify the clamped re-tier applies WITHOUT approval, with a ledger
    entry and a working revert control; with `'propose'`, the same signal
    produces a queued proposal that applies only on approval.
29. (M28) Create a watch over a registered native poll source
    conversationally; verify the compiler names the live source, the poller
    ingests a real item, and the fire produces a delivery.
30. (M29) With the email channel configured against a local SMTP sink,
    trigger a digest flush; verify the digest arrives as one email AND the
    in-app inbox row records the per-channel send; fire a tier-0 interrupt
    and verify the in-app toast appears without a page reload.
31. (M32) Upload an eval csv against a seeded skill; run it; verify graded
    results in the UI, `eval=true` labels on the runs, and (when configured)
    the LangSmith experiment link.
32. (M34) With `auth_enabled=true`, log in as two different users; verify
    conversations, routines, and deliveries are invisible across users,
    registry mutation requires `admin`, and with `auth_enabled=false` the
    §11 byte-identity suite still passes.

**§14d A2A acceptance additions (M37–M39; run with `a2a_enabled=true`
unless stated):**

33. (M37) With `a2a_enabled=false`: the Remote Agents nav item is absent,
    the API 409s on writes, and the §11 byte-identity regression suite
    passes untouched.
34. (M37) Start the local scripted A2A counterparty; register it from the
    Remote Agents page by card URL; the card renders, its skills list,
    and each skill appears on the Tools page as `kind=a2a` with the
    agent-prefixed tool key.
35. (M37) Auth matrix against the counterparty: an apiKey-header scheme,
    http bearer, and oauth2 client_credentials each authenticate; a
    card declaring only an unsupported scheme surfaces auth-unsupported
    and the call fails with a clear tool error; an `env:VAR` credential
    resolves from the environment.
36. (M38) Compose a projected a2a tool into a skill + sub agent; a chat
    run routes to it, the remote answer arrives untrusted-fenced in the
    trace, and the step carries `kind=a2a` labels.
37. (M38) A counterparty task that pauses `input-required` raises the
    standard HITL card carrying the remote question; the typed reply
    resumes the remote task to completion; deny cancels it remotely.
38. (M38) Stop during a live remote call cancels the run AND propagates
    `tasks/cancel` — the counterparty records the cancellation.
39. (M39) A long-running counterparty task exceeds `a2a_task_timeout_s`,
    parks with the structured tool note, and after completion the ambient
    poller delivers the fenced result to the Inbox (category `a2a`) with
    no recheck run.
40. (M37/M39) Card drift: the counterparty adds a skill; refresh-card
    projects the new tool; a parked task's reply/cancel work from the
    Remote Agents task drawer.

**§14e Config-hardening acceptance additions (M40):**

41. (M40) Per-chat pin: pin a sub agent in conversation A; switch to
    conversation B — the picker shows Orchestrator (auto); send in B and
    the run is planner-routed; switch back to A — the pin is restored and
    the next message runs `mode='direct'` against the pinned agent. The
    history-summary checkbox obeys the same per-conversation scoping.
42. (M40) Settings completeness: the Ambient and A2A sections toggle their
    master switches from the page — the corresponding nav entries appear
    and disappear live; every new knob PATCHes and reads back; invalid
    values (out-of-bounds) surface the 422 inline.
43. (M40) Wired knobs behave: raise `a2a_poll_interval_s` above the tick —
    a parked task is NOT rechecked on the next tick and IS after the
    interval elapses; lower `overlap_threshold_percent` — a near-duplicate
    skill save now raises the §4 dialog; change `rate_limit_burst` — the
    429 boundary moves accordingly.
44. (M40) Byte-identity at defaults: with every new key left at its
    default, the §11 suites pass untouched and a fresh boot behaves
    exactly as before the milestone.

**§14f Ambient pursuit acceptance additions (M41; `ambient_enabled=true`,
`ambient_channels` routing `interrupt` at both a local SMTP sink and an
SMS-gateway-shaped webhook sink, outside quiet hours unless stated):**

45. (M41) **Pursuit `away`, someone watching**: with a browser holding
    `/api/v1/ambient/stream`, a tier-0 delivery fires the in-app toast and
    the external channels do NOT send — the row's `external` ledger stays
    empty, and neither sink receives anything.
46. (M41) **Pursuit `away`, nobody watching**: with no subscriber, the
    same tier-0 delivery reaches both sinks — the email lands in the SMTP
    sink, the SMS-shaped envelope lands in the webhook sink, and the row's
    `external` ledger records `ok` per channel. The delivery is otherwise
    identical (same tier, same budget debit, same Inbox row).
47. (M41) **Subordination and the tri-state**: inside quiet hours the same
    tier-0 is demoted to digest and NEITHER a toast nor an external send
    occurs (pursuit escalates the channel, never the hour); with
    `'off'` and nobody watching, nothing external fires; with `'always'`
    and someone watching, the external channels fire exactly as they did
    pre-M41 — the byte-identity leg.

**§14g Delivery-salience acceptance additions (M42):**

48. (M42) **The record stops lying**: with nobody watching and no external
    channel configured, a tier-0 delivery records
    `in_app: {ok:false, "no subscriber"}` and logs `ambient_delivered_unseen`
    — while the same delivery WITH someone watching leaves `external` null
    (byte-identity), and a digest flushing to an empty room records nothing.
    Opening the item in the Inbox sets `seen_at` and decrements the unread
    badge on the Ambient nav.
49. (M42) **Salience escalates**: with `ambient_salience_mode='auto'`, an
    unseen high-urgency alert whose content the judge deems consequential
    is promoted to lead the next digest — and the ledger shows the verdict,
    reason, and confidence. It is NOT re-interrupted, and quiet hours are
    not broken.
50. (M42) **Salience retains and drops**: a consequential unseen item's
    content lands in §16 memory through the normal admission path, carrying
    delivery provenance and visible on the Memory page; a low-value unseen
    item is dropped with an explicit ledger entry. With the judge forced to
    fail, both items keep exactly the disposition they already had
    (fail-open), and adversarial delivery content reaches the judge fenced.
51. (M42) **Nothing else moved**: with `ambient_salience_mode='off'` (the
    default) the §11 suites pass untouched, and a **randomized sample of
    previously captured acceptance frames**, re-captured on the M42 build,
    matches the archived frames surface for surface.

**§14h Salience decision surface + settings completeness (M43):**

52. (M43) **A proposal is actionable**: with `ambient_salience_mode='propose'`,
    an unseen alert's verdict renders on its own Inbox card, leading with the
    consequence in plain language and offering **Do it** / **Leave it**;
    "why this?" expands to the reason, confidence, mode, and the fact that a
    model made the call. **Do it** executes the verdict (an escalation leads
    the next digest, never re-interrupting); **Leave it** changes no state.
    Both record the decision AND the judge's own reward (`judge_reward`
    +1/−1 on the salience record) — the delivery's §17.7 feedback stays
    untouched, so judging the judge never pollutes category precision.
    Replaying a decision is a no-op; a conflicting one is refused.
53. (M43) **An applied verdict is reversible**: with
    `ambient_salience_mode='auto'`, an auto-applied verdict shows what it did
    and offers **Undo** — restoring the row exactly as snapshotted, and
    retracting only the memories that retention itself created. Once the
    escalated item has actually gone out in a digest, Undo refuses with that
    reason instead of pretending. Cross-tenant, every decision route 404s.
54. (M43) **No hidden controls**: every §3.7 role model has a select plus
    params on the Settings page in the section owning that role — including
    `memory_extraction_model` and `ambient_salience_model`, both of which the
    settings API already validated while no UI offered them. Provider API
    keys remain absent from the UI by design (§13).

**§14i Durable forgetting (M44):**

55. (M44) **Forget keeps its promise**: with `memory_forget_enabled` on,
    Forget a memory from the Memory page; the row is gone, a metadata-only
    tombstone appears in the Forgotten section, and a later run mentioning
    the same fact (verbatim AND paraphrased, embedding model configured)
    does NOT re-admit it — the suppression is logged content-free and the
    tombstone's times-suppressed count climbs. **Unforget** deletes the
    tombstone and the next mention admits normally. Explicitly re-stating
    the fact via `memory.remember` overrides the tombstone in one step.
56. (M44) **Erase means erase**: Erase leaves no row, no tombstone, no
    trace; with the master off, deletion behaves exactly as pre-M44
    (byte-identity), and `mode=forget` is refused naming the setting. §8.7
    purge removes memories and every tombstone. Tombstones are
    tenant-scoped: another user's identical fact is not suppressed.
57. (M44) **Proposal rejection is captured**: in §17.7 propose mode, reject
    a pending policy proposal — it lands `status='rejected'` with a
    timestamp, nothing is applied, and the row no longer sits pending
    forever. Capture-only: no learner consumes rejections yet.
58. (M46) **A model switch re-embeds and flips**: with memories stored
    under embedding model A, change `embedding_model` to B in Settings —
    the backfill job embeds every live memory, digest, and exemplar under
    B's key (batched, without deleting A's rows), and vector recall
    answers under B. A row whose write-through embed failed is repaired
    by the same job. Forgotten-fact tombstones stay on hash+anchor
    matching for pre-switch stones — the trade is stated, not hidden.
59. (M47) **A repudiated kind goes through review**: with
    `memory_extraction_learning=propose`, forget five-plus machine
    memories of one kind (majority of that kind's writes) — the tuner
    queues a `memory_quarantine_kinds` proposal in the §17.7 review list;
    nothing changes until approval. Approve it: the next machine write of
    that kind lands in the review queue with the learner's note, while a
    user-stated write of the same kind still activates directly. The
    Settings chip removes the routing in one click.
60. (M47) **The floor answers low-confidence junk**: in `auto`, a stream
    of forgets concentrated just above the current admission floor walks
    `memory_admission_min_confidence` up in 0.05 steps — each move a
    ledgered policy row naming the band evidence — and the walk stops at
    the first band that is mostly kept. High-confidence forgets alone
    never move it (the ratchet the band rule exists to prevent), and it
    never leaves [0.5, 0.9].
61. (M48) **Every autonomous behavior can be silenced by name**: with
    ambient and memory on, turn off `ambient_anticipation_enabled` — the
    idle window passes and no briefing is composed, while watches, digests
    and salience keep working. Turn off each of the four memory job gates
    and confirm from the job result that the named job stops running while
    the others continue. Turn every gate back on: behavior is identical to
    before the switches existed.
62. (M48) **"Off" means off**: set `memory_community_budget_tokens` to 0 —
    the community rebuild stops making summarization calls, not just the
    injection. Enable `memory_forget_enabled` with no `embedding_model`
    configured — the Settings page states plainly that suppression is
    exact-hash only and paraphrases will be re-admitted.
63. (M48) **The eval surface is gateable**: with `evals_enabled` false,
    every `/evals` route answers 409 naming the setting; flipping it back
    restores the surface with datasets and prior runs intact.

## 15. Evals (M32 — promoted from post-POC to in-scope)

Originally deferred; the design below is now implemented as milestone M32,
unchanged in shape. Gated by `evals_enabled` (default true, M48 §3.7.1):
the surface is passive — nothing runs until a dataset is uploaded and a run
started — so the gate exists to remove surface area on a deployment that
does not want it, not to change behavior. Off ⇒ every `/evals` route
answers 409 naming the setting.

- **Eval definition**: spreadsheet upload (xlsx/csv) in a predefined format — columns: `level (skill|sub_agent)`, `target_id`, `input` (task/query), `expected` (reference answer or criteria), `judge_notes` (optional grading guidance). Uploaded and run from the skill / sub agent detail pages.
- **Harness = existing machinery**: skill-level evals run the target as a single-skill ephemeral worker (the rung-4 factory path, unchanged); sub-agent-level evals invoke the full compiled agent. Config snapshots make every eval row reproducible against the exact definition evaluated; eval runs are ordinary runs tagged `eval=true` in the §10 label set, HITL nodes auto-approved in eval mode.
- **Results publishing**: to LangSmith — local instance or remote. Endpoint and enable-toggle via `app_settings` (`langsmith_enabled`, endpoint key); **API key stays env-only** (`LANGSMITH_API_KEY`), consistent with the no-keys-in-DB/UI rule, even when the user supplies a remote key. With LangSmith disabled, eval runs are still fully recorded in Postgres run traces and structured logs — only the dataset/experiment publishing step is skipped.
- **Invocation is admin-direct, not planner-routed**: the eval runner
  builds the target worker directly by registry id (the same compile path
  §4's save-time validation uses) or invokes the sub agent directly (§7.5
  machinery). The §7.1 rung-4 "exposed skills only" rule governs
  planner-initiated composition and does not gate admin-initiated eval
  runs — hidden skills are evaluable.
- **Graders**: per-case `grader ('exact'|'contains'|'llm_judge', default
  'llm_judge')`. `exact` = normalized string equality; `contains` =
  case-insensitive substring; `llm_judge` = one structured call on the
  extraction-model role returning `{passed: bool, score: 0..1, reason}`,
  with `judge_notes` injected as grading guidance. Judge failure grades
  the case `error`, never silently passes.
- **Storage + surfaces**: `eval_datasets` (id, name, level, target_id,
  created_at), `eval_cases` (dataset_id, input, expected, judge_notes,
  grader), `eval_runs` (dataset_id, status, config snapshot, totals,
  langsmith_url nullable), `eval_results` (eval_run_id, case_id, run_id,
  passed, score, grader_reason). Alembic migration per schema change, as
  everywhere. UI: an Evals page (upload csv/xlsx, run, per-case results
  table with pass/fail chips and grader reasons) plus a launcher on the
  skill / sub agent detail drawers.
- **Why the POC already supports this**: the factory builds arbitrary single-skill workers, snapshots freeze configs, settings hot-reload, and the trace label set carries tier/kind/entity — the eval feature is an upload parser, a batch runner, and a publisher. No schema or architecture change anticipated.

## 16. Memory Layers

Design rationale, evidence, and alternatives: `docs/research/memory/` (research suite, 2026-08-20). Principles: **dark by default** (`memory_enabled=false` ⇒ byte-identical pre-§16 behavior); **registry citizenship** (agent-facing memory ops are native tools behind the same exposure gates as every tool); **the write gate is the security boundary** (strict admission + provenance, not content filtering); **the user owns the store** (every row visible/editable/pinnable/deletable; provenance mandatory on machine writes); **no new services** (one image pin: `pgvector/pgvector:0.8.6-pg16` — official postgres image + extension; registries stay JSONB-embedded, untouched); **middleware precedence unchanged** (§7.0 stays at exactly three custom middlewares — memory injection happens at prompt assembly).

### 16.1 Storage

Tables (Alembic, one migration): `memories`, `memory_embeddings` (side-table: `memory_id`, `model_key` ('provider:model@dims'), untyped `vector`, one partial expression HNSW index per active model — provider-agnostic dims, zero-downtime model switch), `memory_entities`, `memory_entity_links`, `run_digests`, `conversation_rollups`, `plan_exemplars`, `routing_stats`.

`memories`: scope ('global'|'conversation') + `conversation_id`, kind ('fact'|'preference'|'entity'|'relation'|'instruction'), `text`, `payload jsonb` (relation s/p/o, preference k/v), `entity_key`, `importance` (1–10, write-time), `confidence` (0–1), source ('extracted'|'user_stated'|'user_edited'|'hitl_note'|'inferred'), status ('active'|'quarantined'|'superseded'|'expired'|'rejected'), bi-temporal columns (`valid_from`/`valid_to` event time; `recorded_at`/`superseded_at` ingestion time), `supersedes`/`superseded_by` chain, provenance (`run_id`/`step_id`, mandatory on machine writes), `last_accessed_at`/`access_count`, `pinned` (always injected + decay-immune), `half_life_days` (NULL = setting default), generated `fts` tsvector. Invariants: pipelines never hard-delete (supersede/expire only; hard delete is a user/purge action); `status='active'` rows form the current view (partial indexes); supersession is append-only (insert replacement + close old row in one transaction, `WHERE superseded_at IS NULL` guard); memory rows are not registry rows (§4 unchanged).

**Durable forgetting (M44).** A physical delete plus existence-only
reconciliation means the system can re-learn a fact the user deleted —
deletion silently promised less than it appeared to. User deletion
therefore splits into two verbs with different guarantees:

- **Forget** (the primary action when `memory_forget_enabled` is on) —
  deletes the row and writes a `memory_tombstones` record: metadata only
  (`user_id`, scope/`project_key`, kind, source, confidence-at-admission,
  importance, age-at-forget, `access_count`, `pinned`, `forgotten_at`,
  `suppressed_count`/`last_suppressed_at`) plus a SHA-256 of the
  normalized text and hashes of its distinctive payload tokens, plus — when the row had one — a copy of its embedding,
  retained **only** for suppression matching and destroyed with the
  tombstone. Never the text itself.
- **Erase** (always available, and the only behavior when the master is
  off) — today's physical delete, no trace of any kind, including no
  record that something was removed. Privacy by the user's explicit
  choice. §8.7 purge erases memories AND all tombstones.
- **Unforget** — deleting a tombstone from the §8.8 Forgotten section;
  the fact becomes learnable again (the memory itself is already gone).

The tombstone taxonomy, threat notes (hash dictionary-attack caveat,
embedding quasi-content), and the decision record live in
`docs/research/feedback_traces/`. Settings: `memory_forget_enabled`
(default false — byte-identical: deletes stay physical, `mode=forget` is
a 422 naming the setting) and `memory_forget_similarity` (default 0.85,
range 0.5–1.0; calibrated live — a real paraphrase pair measured 0.876). Beyond suppression, tombstones have exactly one
consumer: the M47 extraction tuner (`memory_extraction_learning`), which
entered under the §17.7 feedback-consumer rule as that clause required.

### 16.2 Lifecycle

Post-run, async, debounced until the conversation goes quiet, never blocking the answer, never holding a DB transaction across an LLM call: digest + rollup (L1) → extraction (`prompts/memory_extract.md`, structured output via §2.1) → **deterministic admission gate** (confidence floor — the live `memory_admission_min_confidence` setting since M47, default equal to the constant it replaced; near-duplicate drop by embedding distance; kind/scope allowlist; the M47 kind router — a machine write whose kind is listed in `memory_quarantine_kinds` lands quarantined with a review note instead of activating, user-stated writes never routed; and — M44, when `memory_forget_enabled` — the **tombstone check**: a candidate whose normalized-text hash matches a live tombstone is **suppressed**; else the **hybrid gate** decides — cosine ≥ `memory_forget_similarity` (default 0.85) suppresses alone, and in the gray band (cosine ≥ 0.70) suppression additionally requires a shared **distinctive-token anchor** (the tombstone also keeps hashes of payload tokens: ≥10 chars, or ≥6 carrying digits/separators — same privacy tier and caveat as the text hash). Calibrated live in stage 32: real paraphrase pairs measured cosine 0.876 and 0.847, so a lone threshold cannot separate 'same fact restated' (shares its payload token) from 'same topic, different fact' (carries a new one — and must stay admissible, or forgetting an outdated value would block learning its replacement). Suppression — not written, `memory_admission_suppressed` logged content-free, the tombstone's `suppressed_count`/`last_suppressed_at` bumped; matching is tenant- and scope-aware like recall, degrades to hash-only with no embedding model, and applies to machine paths only — a **user-stated** write re-asserting a forgotten fact deletes the matching tombstone and admits, ledgered as unforget-by-assertion: the human's newer word beats their older one) → per-candidate reconciliation: the LLM answers only *same fact / related / unrelated* against hybrid-nearest active neighbors (`prompts/memory_reconcile.md`); **deterministic code resolves** (same fact + newer event time → bi-temporal supersede; ambiguous timing → quarantine both; unrelated → add). Extracted or `inferred` **instruction-kind memories always quarantine** until approved in the review queue; only explicit user-stated instructions via `memory.remember` activate directly. Scheduler: lifespan asyncio task, `pg_try_advisory_lock` per job class (one replica works), jobs = digest/rollup, extract/reconcile, decay sweep (`importance·exp(−Δt_access/half_life)` below floor → 'expired', pinned immune; gated by `memory_decay_enabled`), reflection (importance-sum trigger; synthesized `inferred` memories carry evidence citations to source ids), contradiction sweep (`memory_contradiction_enabled`), routing-stats/exemplar harvest, embedding backfill (on `embedding_model` change). Per §3.7.1 every job that runs on its own schedule carries its own gate; the master alone is not sufficient, because expiring a memory, quarantining one, hard-deleting a digest and spending tokens on a summary are four different consequences. Every job emits §10-labeled events + metrics; mutations fire the §7.3 NOTIFY discipline (≤8KB, ids only, cache-hint semantics).

### 16.3 Retrieval & injection

Scoring: two-CTE RRF in SQL (GIN `ts_rank_cd` + HNSW cosine over the active `model_key`) re-scored by `w_rel·similarity + w_imp·importance/10 + w_rec·exp-decay(last_accessed)`; per-surface token budgets; **score floor** below which nothing injects; **pinned rows always inject** under `memory_pinned_budget_tokens`; **time-aware retrieval** (temporal phrases expand into validity-interval filters); batched access-bookkeeping updates. Surfaces: planner prompt + agentic concierge system prompt (graph aggregator deferred to observation) gain a fenced "remembered context" block — rendered as data, never instructions, with memory ids and one fixed abstention line ("if memory does not cover it, say so — never invent a remembered fact"). Direct runs: `include_memories` flag (§7.5). No embedding model configured → memory retrieval runs lexical-only (the §7.4 degradation rule).

### 16.4 Memory tools (registry citizens)

Seeded static native tools, hidden by default: `memory.recall(query, scope?, kinds?, as_of?)`, `memory.remember(text, kind, scope?)`, `memory.forget(memory_id)` (soft-retire; hard delete is UI/purge only). Seeded static skill `memory-keeper` binds all three. §3.3 boundary unchanged: skill loops see them only when bound; the agentic orchestrator sees them only when exposed. All calls are `tool_call` steps with §10 labels. Tool-surface writes follow 16.2's rules (user-asked remember ⇒ `user_stated`; agent-volunteered instruction ⇒ quarantine).

### 16.5 Procedural learning

`routing_stats` per capability (asks handled, completion/deny/failure rates, mean tokens, latency, last used) — registry UI columns; available to the planner behind `procedural_learning_enabled`. `plan_exemplars`: successful plans/todo traces keyed by task-text embedding, harvested only from positively-signaled runs (completed, no deny, no immediate correction), with an ExpeL vote lifecycle (upvote on reuse-success, downvote on reuse-failure, retire at zero); planner prompt gains a budgeted top-2 "similar past asks" block. Fallback mining: recurring fallback-run clusters draft `.skill.md` proposals through doclint + the overlap judge into the review queue; human approval creates a normal dynamic skill. No autonomous registry mutation.

### 16.6 Observability, testing, acceptance

§10 gains tier `memory` with kind ∈ {digest, rollup, extract, reconcile, decay, reflect, contradict, harvest, backfill, recall, inject}; counters (memories by status/kind, ops by job), histograms (job duration, injected tokens, recall latency); no memory content in logs — ids and counts only. §11 gains: reconciliation matrix, supersession chains, decay math, quarantine rules, budget enforcement, score-floor abstention, purge coverage, and the byte-identity regression (`memory_enabled=false` ⇒ existing suites pass unchanged). §14b defines the acceptance stages; the probe suite records accuracy per ability plus injected tokens and latency (the honest case for memory over long-context is precision + cost). The §15 eval seams are the publishing path for probe results.

### 16.7 Closed-loop refinement (M18)

Measured follow-ups from the M17 experiment (`docs/research/memory/07-experiment-results.md`) — each sharpens an existing layer; none adds a service or a new layer.

**Citation feedback — used beats retrieved.** Injection-path recall no longer bumps access bookkeeping (being retrieved is not evidence of being useful). Injected memory ids are recorded on the run context per surface; a post-run job matches their 8-char id prefixes against the run's final answer (the injected block prints ids precisely so answers can cite them). Cited memories receive the access bump plus an importance reinforcement (+1, capped at 10, at most once per run); injected-but-uncited memories get nothing and cool toward decay naturally. Explicit `memory.recall` tool calls still bump access at call time — deliberate use is use. Fail-open like all consolidation.

**Digest compaction — bounded episodic growth.** Gated by `memory_compaction_enabled` (M48 §3.7.1 — this job hard-deletes, so it is switchable). `run_digests` gains `kind ('run'|'period')`, nullable `run_id`, and `covers_from`/`covers_to`. A consolidation job folds run-digests older than `memory_digest_compact_days` (default 14) per conversation into one synthetic `period` digest (text assembled mechanically, embedded like any digest), then hard-deletes the folded rows and their embeddings — their substance persists in the period digest and the conversation rollup. Episodic recall treats period digests as ordinary candidates. The episodic store becomes O(conversations), not O(runs).

**Entity-hop recall — light graph expansion.** Extraction may name `entities` per candidate; writes get-or-create `memory_entities` rows (case-insensitive by name) and `memory_entity_links`. After hybrid ranking, recall appends up to 2 additional active memories that share an entity with a top hit (deduped, marked as linked, scored at a fixed discount of the weakest direct hit, exempt from the score floor — they were reached by structure, not similarity — but inside the same budgets). No graph database, no multi-hop traversal: one bounded join.

**Long-horizon simulation.** A deterministic time-warp experiment (`experiments/memory/longhorizon.py`) seeds a ~90-day backdated store and drives the decay, reflection, contradiction, and compaction jobs directly, asserting the equilibrium: untouched low-importance rows expire, rehearsed rows survive, pinned rows are immune, the episodic store stays bounded, reflection insights cite evidence. Results append to research doc 07.

## 17. Ambient Mode

Design rationale, evidence, and alternatives: `docs/research/ambient/` (research suite, 2026-08-25; sign-off decisions recorded in doc 06).

### 17.0 Principles

Ambient mode is an **initiation-and-governance mode, not an agent type**: the
same registries, orchestrators, and run/step ledger execute all ambient work;
what changes is who starts a run (a trigger, not a chat message) and the
envelope it runs in (narrowed capability projection, budgets, autonomy
ceiling, delivery policy). Principles carried from §16: **dark by default**
(`ambient_enabled=false` ⇒ byte-identical); **registry citizenship** (ambient
tools/routines behind the same exposure discipline); **deterministic code at
the boundaries** (typed triggers, clamps, caps, timers) with the LLM only
inside framed judgments (significance, rule compilation, content); **the
action gate is the security boundary** (event payloads are untrusted input;
they may start runs, never steer them); **no new services**; **§7.0
middleware precedence untouched** (ambient logic lives in schedulers, stores,
and prompt assembly). Ambient work spans **any registry capability** —
consolidation is one job class, not the mode.

### 17.1 Storage

Tables (Alembic, one migration): `ambient_events` (append-only: kind, source
('schedule'|'webhook'|'poll'|'internal'|'wakeup'|'presence'|'pattern'|'manual'),
payload jsonb UNTRUSTED, dedupe_key, occurred_at, causation_id, correlation_id,
depth smallint default 0, verdict ('fired'|'held'|'expired'|'dropped'),
verdict_reason, routine_id?, intent_id?), `routines` (trusted prompt, triggers
jsonb, capability allowlist as registry refs, model_ref?, autonomy
('propose'|'act_reversible'), budgets jsonb, fire_token_hash, stagger_offset_s,
status, consecutive_failures, last_fired_at), `standing_intents` (text,
condition_type ('event'|'state'|'time'), compiled jsonb, semantic_predicate?,
window jsonb, watermark, cadence_s, current_interval_s + backoff columns,
expires_at, budget jsonb, delivery pref, status), `ambient_wakeups` (run_id?,
routine_id?, due_at, reason, payload jsonb, created_by
('agent'|'system'|'user'), status ('pending'|'fired'|'cancelled')),
`pattern_instances` (rule ref, partition_key, state
('armed'|'matched'|'expired'), a_event_id, deadline_at; unique armed instance
per rule+key), `deliveries` (run_id?, intent_id?, category, tier smallint
0=interrupt/1=notify/2=digest/3=silent, urgency 1-5, title, body,
deliver_no_later_than, delivered_at?, channel?, superseded_by?, feedback
('accepted'|'dismissed'|'ignored')?), `user_presence` (state
('active'|'idle'|'away'|'offline'), last_activity_at, last_heartbeat_at,
visible, updated_at). `runs` gains `trigger jsonb` provenance and
`last_heartbeat_at` (liveness). Invariants: events append-only; routine
definitions immutable for static seeds (status/exposure toggles only, §4
discipline); fire tokens stored hashed, shown once.

### 17.2 Trigger plane

The closed, typed taxonomy (doc 05 FR-T1..T11): schedules (cron/interval/
one-shot, stored UTC, per-routine stagger), webhook fires
(`POST /api/v1/routines/{id}/fire`, per-routine revocable bearer token,
payload always untrusted-fenced at prompt time), pollers (cursor + lookback +
early termination over MCP/native sources, **adaptive cadence**: on quiet
`current = min(current × 1.5, 3600s)`, reset to base 300s on activity;
near-due timers tighten the interval), internal platform events, state
conditions (evaluated on the tick; **state ≠ event at the schema level**),
presence events (idle/returned edges), **agent wakeups** (17.4), manual
fires, NOTIFY pings, MCP subscriptions where available, and composite
patterns (17.3a). Ingestion never blocks a request path; `NOTIFY` is a
wake-up ping only — the drain reads `ambient_events` with `FOR UPDATE SKIP
LOCKED`; the 60s tick sweeps missed pings.

### 17.3 Decision plane

Three tiers strictly ordered by cost: (1) typed matchers — field operators
(equals/contains/starts_with/one_of/regex), event-vs-state semantics, dedupe,
rate caps (per-routine hourly; excess **dropped and counted**, never queued);
(2) significance judge — one structured-output call, defaulting to the
extraction-model role with a per-intent model override for high-stakes
watches; returns
`{significant, urgency 1-5, reason}`; failure ⇒ **held** (silence default);
(3) the run. Every decision writes a fire/hold record `{value, urgency,
attention_state, decision, reason}`. **No LLM call per raw event, ever**
(evidence: doc 03 rule 2). Intervention precision per category is computed
from delivery feedback and surfaces in the UI; persistently low precision
auto-downgrades the category one tier — gated by
`ambient_precision_rule_enabled` (M43c): off means the feedback is still
captured, but no category ever re-tiers behind the user's back.

#### 17.3a Composite patterns (CEP-lite)

Exactly three composite kinds — `sequence` (A then B within T), `conjunction`
(A and B within T), `absence` (A without B by T) — as `pattern_instances`
keyed by partition. **Absence is a timer, not a query**: arming inserts an
instance with `deadline_at`; the tick fires expired armed instances (≤60s
slop). Chaining guards, all four: derived events carry
`causation_id/correlation_id/depth`; `depth ≥ 4` rejected; a rule never fires
if its own id appears in its causation chain (**no self-trigger**); kill
switch at 50 fires/rule/hour (auto-disable + ledger + notification); per
(rule, partition) cooldown default 300s. Routines may not wake themselves
except via the capped wakeup tool.

### 17.4 Execution plane

A fire creates an **ordinary run** (either orchestrator mode) with `trigger`
provenance, the routine's narrowed registry projection, per-run budgets
(max steps/tokens/wall-clock/side-effects) enforced by the runner, a
tokens-without-progress monitor, and the **abstain instruction** — the
abstained outcome is a first-class result. Autonomy: `propose` (default —
consequential output queues for review) and `act_reversible` (irreversible
action classes still gate). Ambient HITL pauses expire gracefully after
`ambient_hitl_timeout_h` — the question rides the digest, the checkpoint
stays resumable from the inbox.

**Heartbeats, three senses** (doc 05 D3): the 60s advisory-locked tick (H1,
exists); **agent-scheduled wakeups** (H2) via native tools
`ambient.wakeup(delay_or_at, reason)` and `ambient.cancel_wakeup(id)` —
platform clamps delays to [60s, 24h], caps 5 pending + 100/day per routine,
and applies a done-guard at fire time (the routine's last run superseding the
reason ⇒ wakeup expires); on tool failure inside an ambient run the runner
schedules one immediate self-wake with the error in context (Letta pattern)
instead of dying silently; **liveness watchdog** (H3) — ambient runs and
watchers refresh `last_heartbeat_at` each tick; the reaper marks rows stale
past 5 min (3–5× cadence) as `stalled`, auto-pauses the owning routine with a
visible reason, and rescues or fails the orphaned work.

**Standing intents** compile once at creation (NL → typed rule via the LLM,
interpretation echoed back for confirmation) and are evaluated by the
scheduler on their cadence — never remembered in model context (doc 03
rule 6). **Idle work**: the real idle detector (no active runs + no chat for
`ambient_idle_minutes`) triggers consolidation, plus the anticipation job —
predict likely next asks from episodic memory + intents, pre-compute
briefing material, record per-item used/unused, self-prune below the
hit-rate floor.

### 17.5 Delivery plane

All ambient output flows through the `deliveries` outbox with four tiers:
**0 interrupt** (immediate regardless of presence; reserved for run-blocking
gates and hard failures; debited from `ambient_notification_budget_per_day`,
default 3; over budget ⇒ leads next digest), **1 notify** (flush on the next
user-returned edge, bounded deferral `deliver_no_later_than = created + 30
min` enforced by the tick — Horvitz bounded deferral), **2 digest** (default;
flushed at `ambient_digest_times`, default 2/day, and on return from absence
> 1h as one collapsed "while you were away" card stack; micro-absences < 5
min flush tier 1 only), **3 silent** (ledger only — silence is an explicit,
logged decision). `superseded_by` collapses stale items. Quiet hours
absolute. Agents must justify tier ≤ 1 in the run record; the default is
digest (the Gmail-nudge bias: conservative keeps trust). Presence comes from
the client (visibility + throttled activity + 30s heartbeat with immediate
beat on foreground): active / idle (5–30 min) / away / offline (>30 min);
the away→active edge emits `user_returned` into the event stream.
Approvals batch into the digest ranked by risk under a daily escalation
budget. Feedback (accepted/dismissed/ignored) is captured per item.

**Pursuit (M41).** The tier machinery decides *whether and when* a row is
delivered; pursuit decides *which channels carry it once that decision is
already made*. `ambient_pursuit` is `'off'` (in-app only — external
channels never fire), `'away'` (external channels fire only for a batch
whose in-app broadcast reached nobody, §18.4), or `'always'` (default —
external channels fire whenever the routing names them; the pre-M41
presence-blind behavior). Pursuit is strictly **subordinate** to the tier
machinery: it never resurrects a row that quiet hours or an exhausted
notification budget demoted, never re-tiers a row, never debits and never
bypasses `ambient_notification_budget_per_day`, and never changes which
rows flush. A tier-0 failure while you are away at 03:00 is therefore
still demoted by quiet hours and still leads the next digest — **pursuit
escalates the channel, never the hour**. It is a routing modifier, not a
second delivery policy.

**Salience (M42).** Tier and urgency are declared *a priori* by the
producer; nothing re-judges a delivery on the merits of what it actually
says. The salience pass closes that: for a delivery that was **supposed to
reach a human in real time and did not** (tier ≤ 1, `seen_at` null, not
superseded), it asks whether the CONTENT deserves attention or the floor.
Two stages, cheap first: a deterministic prefilter (urgency, category, and
the **recurrence count over the row's `skey` lineage** — a thing that keeps
coming back is evidence in itself, previously collapsed and never read as a
signal), then an LLM-as-judge over the **fenced** body returning
`{verdict, reason, confidence}`. Three outcomes, each written to the §17.6
ledger:

- **escalate** — the row leads the next digest (keeps its urgency, sorts
  first). It is **never re-interrupted**: salience may raise an item's
  place in a queue the delivery plane already chose, never re-open a
  delivery decision, re-tier upward past digest-lead, or break quiet hours.
- **retain** — the content is handed to §16 through the normal admission
  path (`reconcile_and_write`) carrying **delivery provenance**, so a fact
  the agent learned survives even though the notification carrying it was
  never read.
- **drop** — recorded explicitly, honoring the existing rule that silence
  is an explicit, logged decision.

The judge is **advisory and fail-open** (§4 overlap-guard precedent): if it
errors or is unavailable the row keeps exactly the disposition it already
had, and the outbox is never blocked. Delivered content is **untrusted**
(remote-agent output reaches deliveries via §19.6) and passes the §17 fence
before entering the judge's context. `ambient_salience_mode`
(`off`|`propose`|`auto`, default `off`, mirroring `ambient_learning_mode`)
gates the whole pass; in `propose` the verdicts queue for approval instead
of applying. **Digest deliveries are never candidates** — a digest reaching
an empty room is its normal condition, not a failure.

**The decision surface (M43).** A queued verdict that nothing can act on is
a dead end, so `propose` carries a working approval control and `auto`
carries a working reverse gear — the §17.7 `'auto'` precedent, where every
ledgered change is one-click revertible, applied to salience:

- **apply** executes the recorded verdict (escalate → digest-lead; retain →
  the §16 admission path; drop → the row is dismissed by the human, which
  is the only way a delivery is ever marked seen — the system never marks
  its own content seen on the user's behalf).
- **decline** changes no state and records the refusal.
- **undo** reverses an applied verdict — in either mode, human- or
  auto-applied — restoring the row to the state snapshotted before the
  mutation, and retracting exactly the memories that retention created (a
  memory the run already held is untouched). Undo is bounded by physics,
  not policy: once an escalated item has actually gone out in a digest the
  mutation is spent, and the control refuses with that reason rather than
  pretending. It is an affordance, never a setting — nothing may switch off
  the user's escape hatch.

Every human decision is a reward for the JUDGE, not for the delivery:
apply records `judge_reward +1`, decline and undo record `judge_reward −1`,
all on the salience record itself — the judge accrues its own track
record, per verdict, and without it it could never be evaluated, only
trusted. A decision never touches the delivery's §17.7
`accepted`/`dismissed` feedback or the §17.3 precision rule: "the judge
misread this alert" and "this alert was worthless" are different facts,
and undoing an over-eager escalation of a REAL alert must never cast a
vote toward demoting that alert's whole category. Decisions are
first-write-wins and idempotent: replaying the same decision is a no-op,
a conflicting one is refused.

`judge_reward` currently has **no consumer** — it is measurement, accruing
from day one so any judgment of the judge rests on real history. A future
salience learner (auto-tuning the urgency floor or per-category judging
from this ledger) enters under the §17.7 feedback-consumer rule: its own
`off`/`propose`/`auto` gate, born dark, never a side effect of an existing
switch.

### 17.6 Governance, observability, evaluation

Master switch + caps as validated settings (below); `consecutive_failures ≥
3` auto-pauses a routine with reason; run-status honesty distinguishes
infrastructure success / task success / did-something, and "why did my
routine do nothing?" resolves from ledger + fire/hold records. Trust labels
end-to-end; consolidation promotions screened for secrets/PII. §10 gains
tier `ambient`, kinds {ingest, match, judge, fire, hold, wakeup, pattern,
deliver, digest, expire, stall, pause}; counters and histograms per plane;
no payload content in logs. §11 gains: byte-identity with ambient off,
clamp/cap/guard enforcement, absence-timer semantics, watchdog rescue,
tier/budget delivery policy, untrusted-payload fencing. Evaluation: a
simulated-clock scripted-event harness (`experiments/ambient/`) scoring
fire/hold Set-F1 with false-alarm accounting, reaction time, and token cost
per configuration, plus a live multi-day soak.

### 17.7 Adaptive policy learning (M25)

The delivery feedback substrate (accepted/dismissed/ignored per item, doc 05
FR-V4) feeds a bandit-style learner that proposes policy adjustments:
shifting `ambient_digest_times` toward observed acceptance windows,
re-tiering categories (a chronically dismissed interrupt category down; a
consistently accepted digest category up), and tuning per-intent judge
thresholds. Governed by `ambient_learning_mode`: `off` (default — collect
only, dark-by-default discipline), `auto` (**the primary mode**: adjustments
apply immediately with no approval step, within hard clamps — digest times
move ≤ 2h from configured, tiers move one step at a time, never into tier 0
— every change ledgered and one-click revertible), `propose` (optional
cautious mode: each adjustment lands in the review queue and activates on
approval — the §16.2 quarantine pattern; M44: a pending proposal can also be
explicitly **rejected** — `status='rejected'`, timestamped, capture-only —
because a rejected proposal is itself feedback about the learner, and
before M44 that signal could only be expressed by letting rows rot). Both modes are fully implemented;
auto is not gated behind propose. The reward
is a blend: acceptance + downstream usefulness (the delivered item's run/
memory was later referenced) − explicit-dismissal penalty, with a
repetition-decay term (recovering-bandit shape); pure acceptance optimization
is forbidden by construction. Learner runs as a consolidation-class job;
byte-identical when `off`.

**The feedback-consumer rule (M43c).** Capture and consumption are
different acts and are governed differently, everywhere in the system.
*Capture is always-on*: recording what a human accepted, dismissed,
decided or undid is inert audit data — cheap, private to its ledger, and
the raw material every future learner and precision metric depends on; a
capture kill-switch would only blind the system retroactively. *Every
consumer is individually gated*: any code path that changes observable
behavior because of captured feedback carries its own Settings control —
the §17.7 learner under `ambient_learning_mode`, the §17.3 static rule
under `ambient_precision_rule_enabled` — and **no consumer ever ships
ungated**. A new feedback-driven behavior enters dark (`off` or
`propose`-first, per the §17.7 mode shape), never hot.

The first such consumer is the **salience tuner (M45)**: gated by
`ambient_salience_learning`, it reads the M43b `judge_reward` ledger per
category (window 20, min sample 5) and proposes exactly two things —
**per-category mutes** (endorsement ≤ 0.10 over ≥ 5 decided; stored as
`salience:<cat>` policy rows, so revert un-mutes, reject stays inert, and
the review UI works unchanged) and **urgency-floor moves** (±1 per
invocation, clamped to [2, 5], via `setting:ambient_salience_min_urgency`
proposals through the same `_apply_special` path as digest-time shifts).
Deterministic rules, deliberately not a bandit — salience decisions are
rare events (decision record: `docs/research/feedback_loop/`). Its
admission evidence is the harness in `experiments/feedback_loop/`:
learner ≥ the best static point at zero missed-critical with zero clamp
violations. Deliberate
The second consumer is the **extraction tuner (M47)** — the learner the
§16.1 no-consumer note reserved: gated by `memory_extraction_learning`,
it reads machine-write tombstones (with their confidence-at-admission
metadata) plus quarantine review rejections, MACHINE_SOURCES only — the
human's own words are consent, never a training signal. Two adjustments:
**kind routing** (a kind ≥ 50% repudiated over ≥ 5 is added to
`memory_quarantine_kinds`, sending future machine writes of that kind
through the §16.2 review queue — novel junk of a repudiated kind is
exactly what tombstone suppression cannot catch) and **admission-floor
moves** (±0.05 clamped [0.5, 0.9] via
`setting:memory_admission_min_confidence` proposals through the same
`_apply_special` path). The floor trigger is band-local — it rises only
when the band a bump would newly refuse is itself ≥ 60% repudiated —
because a raw forget-rate trigger ratchets the floor against
confidence-independent repudiation until it starves valuable kinds (the
harness finding; `experiments/feedback_loop/extraction_eval.py`, two
worlds, counter-evidence in the report). Undo is the Settings field
itself (§8.7 completeness) plus per-kind chips that clear routing.

Explicitly still non-loops, and non-loops stay non-loops without an
explicit spec change: HITL and A2A
approvals are consent gates, not preference signals — learning "the user
always approves, stop asking" is automation creep on a safety mechanism
and is forbidden as a side effect; and §16.1 **Erase** (with the M44
split, the surviving physical verb) leaves no trace by explicit choice,
so an erased memory is deliberately not a captured signal — only
**Forget**, which the user chose knowing it keeps a content-free
tombstone, feeds the M47 tuner.


## 18. Completion Wave (M26–M36)

Everything the M13–M25 campaigns consciously deferred, promoted to
in-scope. Discipline unchanged: dark-by-default for anything that changes
observable behavior, registry citizenship, env-only secrets, no new compose
services, tests first, live milestone proofs on a real model. Every schema
change in this section lands with its own Alembic migration (§13), named
in the milestone that introduces it.

### 18.1 Ambient completeness (M26)

- **Per-routine model**: `routines.model_ref` (exists since M20) is honored
  at execution — `prepare_run` copies it into `trigger.model_ref`; the
  runner overlays it onto the settings snapshot's `default_model` for that
  run only. Validated against the provider registry at routine save.
- **Near-due tightening** (§17.2 sentence, now real): a poller whose intent
  `expires_at` falls within 2× the current adaptive interval polls at
  `base_interval_s` for the remainder — deadlines are never missed because
  AIMD had backed off.
- **Escalation budget** (§17.5 sentence, now real): digest items in
  approval categories (`hitl`, `learning`) are ranked by urgency — the
  judge's 1–5 urgency IS the POC's risk signal, so "ranked by risk"
  (§17.5, §8.9) resolves to urgency ranking here — and only
  `ambient_escalation_budget_per_day` of them flush per day; the overflow
  stays pending for the next digest. The debit is counted from flushed
  approval items that day.
- **Per-item anticipation**: each predicted ask is its OWN tier-2 delivery
  (shared `skey` prefix per idle window), so used/unused feedback and the
  hit-rate floor operate per item, as §17.4 always said. Gated by
  `ambient_anticipation_enabled` (M48 §3.7.1): anticipation is the only
  feature that initiates contact without being asked, so silence must be
  a setting the user can state, not only an outcome the hit-rate floor
  eventually learns.
- **Learner threshold recovery**: `budget.min_urgency` moves DOWN one step
  (never below the default 2) when a watch's mean reward clears the promote
  threshold over the promote sample — thresholds are a dial, not a ratchet.
- **Multi-time digest shifting**: accepted digest deliveries are assigned
  to their nearest configured digest time; each time shifts independently
  toward its own cluster mean, each under its own ±2h anchor clamp.
- **Judge cost accounting**: the significance judge runs
  `with_structured_output(include_raw=True)`; `usage_metadata` from the raw
  message feeds a dedicated `ambient_judge_tokens` counter (§10), the
  structured log line, and a registerable usage hook the M24 harness uses
  to report real token cost per configuration.

### 18.2 Memory context (M27)

- **Cross-fire continuity**: `routines.include_memories` (default false) +
  `routines.conversation_id`. When set, every fire reuses ONE persistent
  conversation for that routine (created on first fire). The continuity
  mechanism is conversation history + rollups accruing across beats;
  §16.3 memory injection for graph/agentic surfaces remains governed by
  `memory_enabled` exactly as for interactive runs. The run rows carry
  `include_memories=true` (consumed by the §7.5 direct-run surface and
  recorded as provenance). Fresh-conversation-per-fire stays the default.
- **`project` scope**: `memories.project_key` (nullable);
  `scope='project'` requires it. Conversations gain an optional
  `project_key`; chat accepts it at conversation creation; recall filters
  and injection include project-scoped memories for matching conversations.
  Memory tools accept `scope='project', project=<key>`.
- **Aggregator surface**: the graph-mode aggregator prompt gains the same
  fenced, budgeted remembered-context block the planner has (§16.3), with
  the same abstention line and citation-id tracking.

### 18.3 Real trigger sources (M28)

Poll-source contract becomes `fn(watermark, config) -> (items, watermark)`;
a standing intent's `compiled.poll` is `{source, config}`. Native sources
registered at boot: `http_json` (URL + items JSON-path + id field;
watermark = last id/hash; egress via the normal client), `rss`
(RSS/Atom via stdlib XML; watermark = newest entry id/date), `mcp_tool`
(server + tool + args + items path — polls through the MCP manager,
"MCP subscriptions where available" in its POC form). State-probe contract:
`fn(config) -> float`; a state intent's `compiled` is `{probe, config, op,
value}`. Native probes registered at boot: `workspace_disk_pct` (config:
path, default the workspace volume), `pending_hitl_count`,
`runs_failed_last_hour`. The watch compiler's prompt lists both live
registries with their config shapes, and its structured output gains
`poll_config`/`probe_config`. A broken source still never kills the tick.

### 18.4 Delivery channels (M29)

A channel adapter registry (`in_app` always; `email`, `webhook`) behind
`ambient_channels` — per-tier routing, e.g. `{"interrupt": ["in_app",
"webhook"], "digest": ["in_app", "email"]}`, validated against registered
adapters. Email renders a digest batch as ONE message over SMTP
(`SMTP_HOST/PORT/USER/PASSWORD/FROM/TO` env-only, consistent with the
no-secrets-in-DB rule); `webhook` POSTs a JSON envelope to
`AMBIENT_WEBHOOK_URL` (the SMS/push-gateway shape — any provider bridge
terminates there). Sends are recorded per channel on the delivery row
(`external` jsonb: channel → ok/error + timestamp); a channel failure logs
and never blocks the in-app outbox. **Toast**: a global
`/api/v1/ambient/stream` SSE endpoint broadcasts delivery events; it
exists only while `ambient_enabled` is true (409 when dark, mirroring the
fire endpoint) and the UI subscribes only when the settings snapshot says
ambient is on — with ambient dark there is no stream, no subscription, no
toast. With ambient on but NO external channel configured, delivery
behavior (tiers, budgets, outbox rows) is byte-identical to M23–M25; the
toast is a rendering of flush events that already occur.

**Pursuit and the presence oracle (M41).** Routing alone is
presence-blind: a configured `email` on `interrupt` sends whether or not
the toast already landed in front of you. `ambient_pursuit` (§17.5, §3.7)
gates the external half of the dispatch on whether the in-app half reached
anyone. The oracle is **the SSE subscriber set itself, sampled at
dispatch**: `_publish` fans out to exactly the subscribers registered in
this process, so "the broadcast reached zero subscribers" is not an
estimate of presence — it is the literal audience of the toast just sent.
This keeps the rule correct with no second source of truth, and it stays
correct under §18.9 multi-replica: the hub is per-process, so a tick on
replica A can only ever toast A's subscribers, and A's count is exactly
what A delivered to. The `ambient_idle_minutes` presence timer (§17.5) is
deliberately **not** the oracle — it answers "has the user been clicking",
not "did the toast land", and would escalate against an open, actively
watched tab that simply had not been clicked inside the idle window.
Known and accepted: a forgotten background tab counts as watching, biasing
pursuit toward fewer external sends — the conservative direction, since
the row is in the Inbox either way. The per-channel `external` ledger
records what actually fired, so a suppressed escalation is visible as an
absent entry rather than a silent one.

**The in-app ledger entry (M42).** Until M42 a flushed row was stamped
`delivered_at` + `channel` even when the in-app broadcast reached zero
subscribers and no external channel was configured — the record asserted a
delivery that reached nobody, and the §17.7 reward substrate could not tell
"seen and ignored" from "never seen". `in_app` therefore becomes a
first-class ledger entry, written **only when it did not land**
(`{"in_app": {"ok": false, "error": "no subscriber", "at": …}}`) and only
for the real-time modes `interrupt`/`notify`. The happy path still leaves
`external` null, so byte-identity at defaults is preserved; a digest
flushing to an empty room writes nothing, because that is its normal
condition. `seen_at` on the delivery row records the moment a human
actually opened it, and the Ambient nav carries the unread count — together
they turn "was this attended to" from an inference into a fact, and they
are what the §17.5 salience pass keys on.

### 18.5 Ambient UI completeness (M30)

M23 delivered §8.9's four tabs functionally; M30 closes the remaining
letter of §8.9 — the pieces below replace their POC stand-ins (JSON
textareas, flat ledger rows, chat-only watch authoring) without changing
the M23 information architecture. Routines: a typed trigger builder (schedule kind pickers, webhook filter
rows with the §17.3 operator set) alongside the raw-JSON escape hatch, and
the drawer shows the routine's run history (`GET /runs?routine_id=`).
M30 also closes the decision-plane gap those rows exposed: webhook fires
are matched against the ROUTINE's stored webhook-trigger filters (schedule
events already embed theirs), so a filter authored in the builder is
actually evaluated on every fire. Ledger: rows expand into a correlation-chain view (events grouped by
`correlation_id`, indented by depth, cause → effect); each category's
precision renders as an inline sparkline over its judged window. Watches:
author from the page — `POST /watches/compile` reuses the `ambient.watch`
compiler (NL → typed rule + echo), the user confirms in the UI, and typed
event-filter watches can also be built directly with the filter rows.

### 18.6 Memory communities (M31)

The Zep-style upgrade deferred at M13: a consolidation-class job runs label
propagation over `memory_entity_links` (deterministic tie-breaks), stores
`memory_communities` (members, label, generative summary via the extraction
model, updated incrementally), and recall gains community breadth — when a
recalled entity belongs to a community, the community summary is eligible
for injection under its own budget line. Communities are rebuilt
incrementally on entity-link changes, never per-query. Dark unless
`memory_enabled` AND `memory_communities_enabled` (M48); empty-graph ⇒
no-op. Per the §3.7.1 corollary, `memory_community_budget_tokens = 0`
means off end-to-end: it skips the rebuild as well as the injection —
before M48 a zero budget silenced the section while the job kept making
one summarization call per changed community.

### 18.7 Custom gateway adapter (M33)

The §2.1 rationale made real: a `custom` provider adapter — OpenAI-
compatible chat-completions gateway, `CUSTOM_GATEWAY_BASE_URL` +
`CUSTOM_GATEWAY_API_KEY` env-only, model list from the validated
`CUSTOM_GATEWAY_MODELS` env var (comma-separated ids — env keeps the sync
`list_models()` port contract, §13; the "its own gateway, its own model
list" scenario). Registered like every provider, passes the shared adapter
contract suite, zero changes outside `app/llm/`.

### 18.8 Auth & tenancy (M34 — dark by default)

`auth_enabled` (env `AUTH_ENABLED`, default false): off ⇒ byte-identical
single-user behavior, the whole §11 suite untouched. On ⇒ `users` table
(scrypt password hashes, `admin|member` roles, and a `prefs jsonb` column),
bearer session tokens (`POST /auth/login`, hashed at rest, TTL from
`AUTH_SESSION_TTL_H` env, default 24h — M40), a FastAPI
dependency guarding `/api/v1` (exempt: health, metrics, `POST /auth/login`,
and `POST /routines/{id}/fire` — the fire endpoint keeps its own hashed
fire-token auth, §17.2, which is not a user session), and a bootstrap
admin whose one-time password prints to the boot log. Tenancy boundary: **registries are shared
and admin-writable** (members read + invoke); **work is per-user** —
conversations, runs, memories, routines, watches, deliveries, presence,
and quiet hours carry `user_id` and are invisible across users. Ambient
scoping follows ownership end-to-end: a routine fires runs as its owner;
digests/budgets/learning are computed per user. Per-user quiet hours and
digest times live in `users.prefs` as OVERRIDES of the global §3.7 keys
(`{"ambient_quiet_hours": [...], "ambient_digest_times": [...]}`); the
global settings remain the defaults and the only values when auth is off —
§3.7 and §17.5 read unchanged in the single-user regime. Basic hardening: per-user
token-bucket rate limit, security headers, CORS pinned to the frontend
origin. Passwords/keys never leave env or hashed columns.

### 18.9 Multi-replica coordination (M35)

The ambient tick elects a leader via a Postgres advisory lock (dedicated
classid) with heartbeat lease renewal; non-leaders LISTEN and drain (the
`FOR UPDATE SKIP LOCKED` drain and executor were replica-safe by
construction) but skip the evaluators; on leader death the lease lapses and
another replica takes over within one tick. Registry-cache invalidation
already rides LISTEN/NOTIFY (M8b). Compose stays three services — scale is
`docker compose up --scale backend=N` behind any port mapping the operator
chooses; correctness is proven in-process with two concurrent loops.


### 18.10 Acceptance ceremony (M36)

Fresh volumes (`./decom.sh -y`), fresh `docker compose up`, default model
`openrouter:qwen/qwen3.8-max` for all roles unless a stage names another
provider. Scope, in order: the original §14 ten-step script; §14c steps
20–32 (ambient, channels, evals, auth); a custom-gateway smoke (§18.7 —
one live chat run through the `custom` provider pointed at a real
OpenAI-compatible endpoint); and the §11 byte-identity suites with
`ambient_enabled=false`, `memory_enabled=false`, `auth_enabled=false`.
Evidence lands in `docs/acceptance/ceremony_m36/` (curl transcripts + UI
frames), and a closing report updates the README status line. The ceremony
re-earns the definition of done end-to-end; nothing ships from this wave
without it.

## 19. A2A Outbound — Calling External Agents (M37–M39)

Design rationale, evidence, and alternatives: `docs/research/a2a/`
(research suite, 2026-08-27; sign-off decisions recorded in doc 06).

Principles, restated for this wave: dark by default (`a2a_enabled=false`
⇒ byte-identical to a build without §19), registry citizenship (remote
capability becomes ordinary `tools` rows and composes through §3
unchanged), deterministic code at the boundaries (auth dispatch, task
bookkeeping, fencing — never prompt-enforced), secrets never echoed
(credentials are write-only; `env:VAR` indirection supported), no new
compose services, and §7.0 middleware precedence untouched — A2A plugs
into `materialize_tool`, not the middleware stack.

### 19.1 Protocol + SDK posture

Outbound only. The official `a2a-sdk` (pinned `>=0.3,<0.4`) is imported
solely inside `backend/app/a2a/`; the rest of the app sees port types.
Internal task states mirror A2A's nine (`submitted`, `working`,
`input-required`, `completed`, `canceled`, `failed`, `rejected`,
`auth-required`, `unknown`) plus a local `parked`, so serving an inbound
Agent Card later is additive. Streaming and polling counterparties are
consumed through one SDK client iterator — no transport branching.

### 19.2 remote_agents registry (M37)

Tables (Alembic, one migration): `remote_agents` (RegistryRecord columns
plus `card_url`, `card` jsonb — the last fetched Agent Card, verbatim —
`card_fetched_at`, `auth_schemes` jsonb projection for the UI,
`credentials` jsonb — write-only, never serialized outward —
`last_error`), `a2a_tasks` (§19.5/19.6 bookkeeping: `remote_agent_id`,
nullable `run_id`, `call_key`, `remote_task_id`, `context_id`, `state`,
`question`, `result` jsonb, `error`, `parked_at`, `delivered`,
timestamps), and nullable `tools.remote_agent_id`. Registration fetches
the card from `<url>/.well-known/agent-card.json`, validates it, stores
it, and ingests; a refresh loop (`a2a_card_refresh_interval_s`) re-fetches
each active agent's card — fetch failure sets `status='error'` +
`last_error`, recovery restores `active`. `remote_agents` is manager-held
like `mcp_servers` and does not join the §7.3 cached registries; its
projected tools do, automatically.

### 19.3 Auth (M37)

The card's `securitySchemes`/`security` declare what the counterparty
accepts; we implement `apiKey` (header/query/cookie), `http` (bearer,
basic), and `oauth2` client_credentials (authlib-backed token fetch with
per-agent cache + refresh). Scheme choice = the card's preference order
intersected with schemes holding stored credentials; an agent whose card
declares only unsupported schemes registers fine but surfaces
`auth-unsupported`, and calls fail with a clear tool error — never
silently unauthenticated. Credential values are stored per agent per
scheme, write-only (no API ever returns them; the UI shows configured/not
per scheme), and a value of the form `env:VAR_NAME` resolves from the
environment at call time for env-only deployments. Interactive OAuth2
(auth-code) and OIDC login flows are out of scope this wave.

### 19.4 Tools projection (M37)

Each card skill ⇒ one `tools` row: `kind='a2a'`, `remote_agent_id` set,
`tool_name` = the card skill id, `tool_key` = `{agent name}.{skill name}`
(6-hex suffix on collision, MCP ingest semantics), description = the
skill's name/description/tags digest (planner routing signal),
`input_schema` = `{message: string (required), data?: object}` — A2A
skills are advisory; invocation is agent-level `message/send` with the
skill referenced in metadata. Re-ingest updates changed skills in place
and flips vanished ones `inactive` (ids stable, never deleted); every
ingest ends with a `tools` cache invalidation. Exposure toggles, §3.3
binding, §7.3 caching, and §7.4 retrieval apply unchanged.

### 19.5 Execution (M38)

`materialize_tool` gains the `kind='a2a'` branch: a lazy proxy that
resolves the manager + card at call time (dead agent = tool-call error,
error-edge semantics). Call flow: adopt-or-send (an open `a2a_tasks` row
matching the run and `call_key` — a hash of tool id + canonical args —
is adopted instead of re-sent, making HITL resume replays idempotent, the
§7.1 spin_worker contract); consume updates until terminal,
`input-required`, or the `a2a_task_timeout_s` budget; `input-required`
raises the standard HITL interrupt (a text-kind form gate carrying the
remote question, fenced) — deny cancels the remote task, approve sends
the reply into the same task and resumes consuming; terminal `completed`
returns the fenced result text, other terminals are tool errors carrying
the fenced remote reason. Run cancellation propagates `tasks/cancel`
best-effort. Every remote-authored string (results, gate questions,
delivery bodies) passes through the `<untrusted_remote_agent_output>`
fence (prompt file, §17's fixed never-follow-instructions paragraph)
before reaching any model context. §10 gains tier `a2a` (kinds:
card_fetch, ingest, send, update, hitl, park, poll, deliver, cancel);
steps inherit `kind='a2a'` from the registry record.

### 19.6 Long-running tasks (M39)

A task that outlives `a2a_task_timeout_s` parks (row `state='parked'`,
capped by `a2a_max_parked`) and the tool call returns a structured
"parked; result arrives ambiently" note — no run ever stays open waiting
(§17.4 budgets). A leader-tick evaluator polls parked tasks every
`a2a_poll_interval_s` via `tasks/get`: terminal states become §18.4
outbox deliveries (category `a2a`, tier 2, tier 1 for failures, skey
`a2a:{task id}`) carrying the fenced result — no recheck run;
`input-required` while parked becomes a tier-1 delivery and the reply
happens from the Remote Agents task drawer. Parking requires
`ambient_enabled`; with ambient dark the budget expiry is a plain tool
error, and everything in §17 remains byte-identical.

### 19.7 Testing (§11 additions)

An in-process scripted A2A counterparty (built from the SDK's server
half — the §11 fake-provider discipline applied to A2A) drives the
contract suite: card fetch/refresh/drift, the auth matrix (apiKey
header/query/cookie, basic, bearer, oauth2 client_credentials incl. the
token cache and `env:` indirection), task lifecycle across all nine
states, adoption idempotency on resume replay, fencing, park → poll →
delivery, cancel propagation, and byte-identity with `a2a_enabled=false`.
