# Memory research 06 — spec amendment draft + milestone plan

> CLAUDE.md: "If you believe the spec is wrong, say so and propose the change
> before coding it." This document is that proposal. Nothing here is
> implemented; it is the text we would merge into `spec.md` (as a new §16 plus
> touch-ups to §3.7, §8, §12, §14) once reviewed.

## A. Proposed new spec section — §16 Memory Layers

### 16.0 Principles

- Memory is layered over existing stores: the runs ledger is the episodic
  substrate, the registry is the procedural substrate; §16 adds the semantic
  store, the lifecycle, and the retrieval/injection plane.
- **Dark by default**: `memory_enabled=false` keeps runtime behavior
  byte-identical to pre-§16 builds. Every injection surface is budgeted and
  flag-gated. (The §7.4 rollout discipline.)
- **Registry citizenship**: agent-facing memory operations are native tools in
  the tools registry — exposure-gated, skill-bindable, traced as `tool_call`
  steps, subject to the same §7.1 composition rules as every tool.
- **Visible store**: every memory row is user-inspectable, editable (edit =
  superseding row), pinnable, deletable. Provenance (`run_id`/`step_id`) is
  mandatory on machine-written rows.
- **No new infrastructure** (§2 unchanged in service count): Postgres tables,
  embeddings via the §2.1 port, asyncio consolidation with
  `pg_try_advisory_lock`, NOTIFY invalidation (§7.3 discipline). One image pin
  change is proposed (below): `postgres:16` → `pgvector/pgvector:0.8.6-pg16` —
  the official image plus the extension, same three services.
- **Middleware precedence unchanged** (§7.0): memory injection happens at
  prompt assembly (planner/aggregator builders, concierge system prompt), not
  via new custom middleware. The sanctioned-custom list stays exactly three.

### 16.1 Storage

Tables (Alembic migration each): `memories`, `memory_entities`,
`memory_entity_links`, `run_digests`, `conversation_rollups`,
`plan_exemplars`, `routing_stats`.

Tables: `memories`, `memory_embeddings` (side-table: `memory_id`, `model_key`,
untyped `vector` — one partial expression HNSW index per active model; the
provider-agnostic dimension strategy, research 04 §6), `memory_entities`,
`memory_entity_links`, `run_digests`, `conversation_rollups`,
`plan_exemplars`, `routing_stats`.

`memories` columns (summary; DDL in research 04 §4 / 05 §4.1): scope
(`global|conversation`), kind (`fact|preference|entity|relation|instruction`),
text, payload jsonb, `entity_key`, importance (1–10), confidence, source
(`extracted|user_stated|user_edited|hitl_note|inferred`), status
(`active|quarantined|superseded|expired|rejected`), bi-temporal columns
(`valid_from/valid_to` event time; `recorded_at/superseded_at` ingestion time),
`supersedes/superseded_by` chain, provenance FKs (mandatory on machine writes),
access bookkeeping (`last_accessed_at`, `access_count`), `pinned` (= always
injected + decay-immune), `half_life_days`, generated `fts` tsvector.

Invariants: the pipeline never hard-deletes (supersede/expire only; hard delete
is user/purge action). `status='active'` rows form the current view (partial
indexes). Registry ids remain immutable (§4 unchanged) — memory rows are not
registry rows. Postgres image: `pgvector/pgvector:0.8.6-pg16` (amends the §7.3
"stock image" sentence; registries stay JSONB, untouched).

### 16.2 Lifecycle

- **Post-run** (async, debounced until the conversation goes quiet, never
  blocking the answer): digest + rollup (L1), then extraction
  (`prompts/memory_extract.md`) → **deterministic admission gate** (confidence
  floor, near-duplicate drop, topics allowlist — write policy alone swings
  downstream accuracy 3×, research 03 §5) → per-candidate reconciliation:
  the LLM (`prompts/memory_reconcile.md`) answers only *same fact / related /
  unrelated* against hybrid-nearest neighbors; **deterministic code resolves
  the winner** (newer event time supersedes bi-temporally; ambiguous timing
  quarantines) — LLM-resolved freshness measures 7–28% vs 78–94.8% for this
  split (research 03 §7). Never a DB transaction across an LLM call.
- **Instruction quarantine**: extracted or inferred `instruction` memories are
  always `quarantined` until approved in the review queue. Only explicit
  user-stated instructions (`memory.remember` invoked on the user's ask)
  activate directly.
- **Scheduler** (asyncio, lifespan-started, advisory-locked per job class):
  decay sweep (Ebbinghaus-style effective importance; below floor → `expired`),
  reflection (idle/weekly: clustered memories → `inferred` higher-order
  memories), contradiction sweep, routing-stats + exemplar harvest, embedding
  backfill on model change. Idle = no active runs for
  `memory_idle_minutes`. Every job emits §10-labeled events and metrics.

### 16.3 Retrieval & injection

- Scoring: two-CTE RRF in SQL (lexical `ts_rank_cd` + HNSW cosine, research
  04 §2) re-scored by `w_rel·similarity + w_imp·importance/10 +
  w_rec·exp-decay(last_accessed)`; per-surface token budgets; score floor
  below which nothing injects; **pinned rows always inject** under their own
  small budget; **time-aware retrieval** (temporal phrases expand into
  `valid_from/valid_to` filters); retrieved rows get batched
  access-bookkeeping updates.
- Surfaces: planner prompt and aggregator prompt (graph), concierge system
  prompt (agentic) — each gains a budgeted "remembered context" block
  (memories + episodic digests + optional exemplars) with a fixed abstention
  line. Direct runs (§7.5): new opt-in flag `include_memories` with
  `include_history_summary` semantics (422 rules, run column, retry
  preservation, byte-identical when off).
- Rendering rule: memory content is fenced as remembered *data*, never as
  instructions; ids are shown so the model can cite or `memory.recall` for
  detail.

### 16.4 Memory tools (registry citizens)

Seeded static native tools, hidden by default: `memory.recall`,
`memory.remember`, `memory.forget`. Seeded static skill `memory-keeper`
binding them. §3.3 boundary unchanged: skill loops see them only when bound;
the agentic orchestrator sees them only when exposed. All calls are
`tool_call` steps with §10 labels.

### 16.5 Procedural learning

- `routing_stats` per capability (completion/deny/failure rates, cost) —
  surfaced in registry UI; available to the planner behind
  `procedural_learning_enabled`.
- `plan_exemplars` harvested from positively-signaled runs; planner prompt
  gains top-2 "similar past asks" few-shots (budgeted).
- Fallback mining: recurring fallback clusters draft `.skill.md` proposals
  through doclint + overlap judge into the review queue; human approval turns
  a proposal into a normal dynamic skill. No autonomous registry mutation.

### 16.6 UI (§8 addition — "Memory" page)

List/search/filter (scope/kind/status/source); edit-as-supersede; pin; hard
delete; quarantine review queue (approve/reject + note, HITL card pattern);
provenance links to run traces; layer counters and last-consolidation status;
skill-proposal review tab.

### 16.7 Settings (§3.7 additions)

`memory_enabled` (default **false**), `memory_extraction_enabled`,
`memory_reflection_enabled`, `procedural_learning_enabled`,
`memory_injection_budget_tokens`, `memory_recall_top_k`, `memory_score_floor`,
`memory_extraction_model` / `memory_extraction_model_params` (nullable →
default model), `memory_half_life_days_default` (per-kind overrides jsonb),
`memory_idle_minutes`. All live-read.

### 16.8 Observability (§10 addition)

New `tier` value `memory` with `kind ∈ {digest, rollup, extract, reconcile,
decay, reflect, contradict, harvest, backfill, recall, inject}`; counters
(memories by status/kind, ops by job), histograms (job duration, injected
tokens, recall latency), and the standard label set on every op. No memory
*content* in logs — ids and counts only.

### 16.9 Testing (§11 addition)

- Unit: reconciliation verdict matrix, bi-temporal supersession chains, decay
  math, quarantine rules, budget enforcement, score-floor abstention.
- Contract: memory tools through the adapter/tooling suites; purge covers all
  memory tables; `memory_enabled=false` ⇒ existing suites pass byte-identical.
- Probe suite (§14 addition, LongMemEval's five abilities + poisoning):
  multi-session recall; preference application; knowledge update (supersession
  honored, old fact not resurrected); temporal question answered from validity
  intervals; abstention when memory is absent; instruction-quarantine
  (extracted instruction does NOT change behavior until approved). **Every
  probe also records injected tokens and latency deltas** — the honest case
  for memory over long-context is precision plus cost, and both get numbers
  (research 03 §6).

### 16.10 Acceptance (§14 additions)

One stage per milestone (below), each with UI evidence; plus a regression
stage: full pre-§16 acceptance top-to-bottom with `memory_enabled=false`.

## B. Touch-ups to existing sections

- **§3.7**: add the 16.7 keys to the settings table.
- **§7.5**: document `include_memories` beside `include_history_summary`.
- **§8**: add the Memory page; registry pages gain the "learned" (routing
  stats) column.
- **§8.7**: purge covers memory tables.
- **§12**: milestone table gains M13–M17 (below).
- **§14**: acceptance script gains the 16.10 stages.
- **§15 (evals)**: the probe suite is the first concrete §15 consumer —
  LangSmith datasets from `plan_exemplars` and probe transcripts.

## C. Milestones (spec §12 style — tests first, one PR each, UI evidence)

| M | Scope | Definition of done |
|---|---|---|
| **M13 — substrate** | pgvector image pin + `CREATE EXTENSION` migration; migrations for all §16.1 tables (incl. the `memory_embeddings` side-table + partial HNSW); memory service (CRUD + two-CTE RRF + composite scoring + admission gate + deterministic supersession); memory native tools + `memory-keeper` skill (seeded, hidden); §16.7 settings; purge extension; obs labels | suites green; `memory_enabled=false` byte-identity proven; tools visible in registry UI (hidden); acceptance stage: create/recall/forget through the tool surface in chat |
| **M14 — episodic** | post-run digest + rollup jobs; scheduler skeleton (advisory locks, idle detector); planner/aggregator/agentic injection blocks (budgeted, flag-gated); `include_memories` for direct runs | cross-conversation recall demo: fact from conversation A retrieved in conversation B via digests; budgets visible in trace; stage evidence |
| **M15 — semantic** | extraction + reconciliation pipelines; bi-temporal supersession; instruction quarantine; Memory UI page incl. review queue; abstention line + score floor | probe subset passes (recall, update-supersession, abstention, quarantine); UI evidence of edit-as-supersede + provenance links |
| **M16 — procedural** | routing_stats + exemplar harvest; planner few-shot block; fallback mining → `.skill.md` proposals through doclint + overlap judge + review queue | **measured**: stage-30 prompt suite re-run; fallback-rate delta reported; one mined skill proposal approved end-to-end in UI |
| **M17 — reflection + evals** | decay sweep, reflection job, contradiction sweep; Prometheus dashboards; full §16.9 probe suite wired into acceptance; docs suite update | all probes green on fresh compose; full regression stage green with memory off; docs + diagrams updated |

Sequencing rationale: M13 is pure substrate (no behavior change), M14 delivers
the first user-visible value with the least model-judgment risk (digests are
mechanical), M15 is the core semantic layer, M16 attacks the measured stage-30
routing defect, M17 closes the loop with decay/reflection and the eval harness.

## D. Decisions settled by the research (no longer open)

1. **Store substrate → native Alembic-managed tables.** Research 04 verified in
   source that `AsyncPostgresStore`'s semantic search sequential-scans (does
   not use the HNSW index it builds) and that its flat schema cannot express
   supersession/bi-temporality/importance; `AsyncPostgresSaver` stays for
   checkpoints. `langmem` is not taken as a dependency (0.0.30, ten months
   stale, hard-depends on `langchain-anthropic`+`langchain-openai` — a §2.1
   provider-isolation violation); its patterns are reimplemented.
2. **Reconciliation = LLM matches, deterministic code resolves** (7–28% vs
   78–94.8%, research 03 §7).
3. **Strict admission gate on every machine write** (3× downstream accuracy
   swing; the mitigation class that survives poisoning — research 03 §5/§7).
4. **Retrieval units**: run-level digests + atomic facts primary; rollups for
   global questions only (fact-level beat summary retrieval 41.4 vs 29.9 F1).

## E. Open questions for review (decide before M13)

1. **pgvector image pin** (`postgres:16` → `pgvector/pgvector:0.8.6-pg16`):
   recommended in 04/05 — same three services, official image + extension —
   but it amends spec §7.3's "stock image keeps working" sentence, so it needs
   an explicit sign-off. Fallback: registry-style JSONB embeddings +
   in-process ranking for the first weeks, at the cost of a rework later.
2. **Scope model**: is `project` scope needed pre-multi-user, or does
   `global|conversation` suffice for the POC? (Proposal: defer `project`.)
3. **Aggregator injection**: inject memories into aggregation, or only
   planning surfaces? (Proposal: planner + agentic first; aggregator in M15
   behind its own budget after traces are seen.)
4. **`include_memories` default for direct runs** once `memory_enabled=true`:
   stay opt-in per run (proposed) or follow a per-agent setting?
5. **Digest model**: default model at effort `low` (proposed) vs a dedicated
   cheap model setting from day one.
