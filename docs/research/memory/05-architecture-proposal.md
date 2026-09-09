# Memory research 05 — architecture proposal

> Synthesis of 01 (what exists), 02 (production systems), 03 (academic evidence),
> 04 (substrate). Designed to be buildable inside the platform's standing
> constraints: three compose services, Postgres as truth, provider layer for
> every model/embedding call, §7.0 middleware precedence, prompts as files,
> tests-first milestones, dark-by-default rollout. Where a rule below leans on
> measured evidence, the number and its source doc are cited inline.

## 0. Design stance

Four decisions shape everything else:

1. **Memory is a set of layers over stores the platform largely already has.**
   The runs ledger *is* episodic memory; the registry *is* procedural memory
   (01). What is genuinely new: the semantic store, the lifecycle processes
   (gate → extract → reconcile → consolidate → decay), and the budgeted
   retrieval/injection plane.

2. **Memory operations are registry citizens.** Agent-performed reads/writes go
   through native tools (`memory.recall`, `memory.remember`, `memory.forget`)
   in the tools registry — exposure-gated, skill-bindable, HITL-gateable,
   traced as `tool_call` steps. The platform's governance story (exposure
   gates, rung-4 composition gate, overlap judge, doclint) applies to memory
   for free. None of the surveyed systems has this property; it falls out of
   the tri-layer registry.

3. **The write gate is the security boundary, not content filtering.** Strict
   admission tripled downstream accuracy over add-all (13.05% → 38.50%; 03 §5),
   and write-time gating + provenance is the only mitigation class that
   survives memory-poisoning attacks (AgentPoison ≥80% success at <0.1% poison
   rate; MINJA >95% via query-only interaction; 03 §7). Every machine write
   passes an admission gate; instruction-kind memories additionally pass HITL.

4. **The user owns the store.** Every memory is visible, editable, pinnable,
   deletable in the UI; provenance links every machine-written row to the run
   that produced it. The hidden-dossier design is the most criticized memory
   UX in the field (02 §6).

## 1. Layer map

```
                        ┌─────────────────────────────────────────────┐
   injection plane      │  L0 WORKING — run/turn context (exists)      │
   (budgeted, per       │  history window · checkpointer · Summariz-   │
   surface, score-      │  ationMiddleware · M11 summary               │
   floored)             ├─────────────────────────────────────────────┤
        ▲               │  L1 EPISODIC — what happened (exists; add    │
        │               │  per-run digests + rollups + retrieval)      │
  planner · agentic     │  runs ledger · run_steps · HITL notes        │
  orchestrator ·        ├─────────────────────────────────────────────┤
  aggregator ·          │  L2 SEMANTIC — durable facts (NEW)           │
  direct workers        │  memories · entities-lite · bi-temporal      │
  (opt-in) ·            │  supersession · quarantine · write gate      │
  memory tools          ├─────────────────────────────────────────────┤
        │               │  L3 PROCEDURAL — how to act (registry exists;│
        ▼               │  add routing stats · plan exemplars with     │
   admission gate →     │  vote lifecycle · fallback→skill proposals)  │
   extraction &         ├─────────────────────────────────────────────┤
   consolidation        │  L4 CONSOLIDATION — reflection · decay ·     │
   (post-run + idle     │  contradiction sweeps · rollups (NEW;        │
   asyncio jobs,        │  the ambient-mode runway)                    │
   advisory-locked)     └─────────────────────────────────────────────┘
```

## 2. L0 — working memory (formalize, don't rebuild)

Keep the 20-run/8 000-char recency window and `SummarizationMiddleware` exactly
as they are. Two additions:

- **Context assembly budget**: the prompt-assembly step gains named, budgeted
  *sources* — `history` (existing), `memories` (L2), `episodes` (L1),
  `procedures` (L3) — each with a token budget from settings, each emitting an
  observability event with counts and token cost. Budgets are the first defense
  against retrieval distraction, which is measured and severe: related-but-
  wrong context costs up to −67% accuracy (03 §6).
- **Injection is prompt-assembly, not middleware.** §7.0 sanctions exactly
  three custom middlewares; memory does not need a fourth. Graph mode: the
  blocks are assembled where `build_history()` output is consumed (planner,
  aggregator builders). Agentic mode: the concierge system prompt gains the
  same budgeted block at `create_agent` construction. Direct mode: memory
  rides only the §7.5 opt-in surface — a new `include_memories` flag mirroring
  `include_history_summary` semantics (default false, byte-identical when
  off).

## 3. L1 — episodic memory (make the ledger readable)

The ledger already stores everything; the additions make it *retrievable at
the right granularity*. The evidence is specific about that granularity:
fact/round-level retrieval beats session-summary retrieval (41.4 vs 29.9 F1;
round-level indexing +5%; 03 §6) — so summaries are for *global* questions and
browsing, never the primary index.

- **`run_digests`** — one row per completed run (round-level): a one-to-two-
  sentence digest of ask + outcome (post-run, default model at effort `low`,
  prompt `prompts/memory_digest.md`), plus mechanically harvested outcome
  signals: final status, HITL approvals/denials + notes, user stop, next-turn
  correction heuristic, tokens, duration, route rungs. Embedded via the
  side-table (§4.1). These are the primary episodic retrieval unit.
- **`conversation_rollups`** — a rolling summary per conversation, updated
  post-run. Serves sense-making ("what have we been working on?") and the
  Memory UI, GraphRAG-community-style (03 §4) — a complement to, not a
  replacement for, digests.
- **Retrieval**: hybrid rank (04 §2 recipe) over digests, filtered by scope,
  fused with recency; surfaced in the planner's context block ("similar past
  episodes") and through `memory.recall`.

## 4. L2 — semantic memory (the new core)

### 4.1 Schema (native tables — decision settled by 04, see §9)

Full DDL in 04 §4/§6; the shape:

```
memories (
  id, scope ('global'|'conversation'), conversation_id?,
  kind ('fact'|'preference'|'entity'|'relation'|'instruction'),
  text, payload jsonb,                -- structured form (relation s/p/o, preference k/v)
  entity_key text?,                   -- what single-valued fact this is "about"
  importance smallint 1..10,          -- write-time LLM score (GA convention, 03 §2)
  confidence real 0..1,
  source ('extracted'|'user_stated'|'user_edited'|'hitl_note'|'inferred'),
  status ('active'|'quarantined'|'superseded'|'expired'|'rejected'),
  valid_from, valid_to,               -- event time (bi-temporal, 02 §3 / 04 §4)
  recorded_at, superseded_at,         -- ingestion time
  supersedes, superseded_by,          -- chain, append-only
  run_id?, step_id?,                  -- provenance, mandatory on machine writes
  last_accessed_at, access_count,     -- rehearsal bookkeeping (decay on ACCESS, 03 §2)
  pinned bool,                        -- pinned = always injected + decay-immune (Letta
                                      --   core-block pattern, 02 §1)
  half_life_days real?,               -- NULL = kind-default from settings
  fts tsvector GENERATED              -- lexical leg (04 §2)
)
memory_embeddings (memory_id, model_key, embedding vector /*untyped*/, embedded_at)
  -- one partial expression HNSW index per ACTIVE model_key; model switch =
  -- advisory-locked background re-embed + CREATE INDEX CONCURRENTLY + flip
  -- (the pgvector-README pattern; 04 §6). Same side-table serves run_digests.
memory_entities (id, name, entity_type, aliases[], ...)
memory_entity_links (memory_id, entity_id)
```

Relations are memories of `kind='relation'` with
`payload = {subject_entity_id, predicate, object_entity_id | object_value}` —
graph-lite. The evidence says this is the right amount of graph: typed links
help multi-hop, but full graph systems add ~nothing on single-hop recall and a
temporal-KG product scored 7% on conflict resolution (03 §4/§7). 1–2 hop
traversal via recursive CTE; full temporal-KG machinery deferred with a
written trigger (entity count or hop-depth needs exceeding what CTEs serve).

### 4.2 Write path — gate, extract, reconcile

Post-run, asynchronously, never blocking the answer, never holding a DB
transaction across an LLM call (04 §5):

1. **Extract** (`prompts/memory_extract.md`, structured output via the
   provider layer): candidate memories from the turn — facts stated,
   preferences revealed, entities introduced, corrections issued. Each
   candidate gets a write-time importance (1–10) and confidence. HITL notes
   are harvested mechanically (`source='hitl_note'`).
2. **Admission gate** (deterministic, code): drop candidates below confidence
   floor, over-generic texts, near-duplicates by embedding distance, and
   anything whose scope/kind the settings disallow (Memory-Bank-style topics
   allowlist, 02 §6). The gate is strict by design — write policy alone swings
   downstream accuracy 3× (03 §5).
3. **Reconcile — LLM matches, code resolves.** For each surviving candidate,
   retrieve hybrid-nearest active neighbors (same scope, kind, entity_key
   first). The LLM answers exactly one narrow question per neighbor: *same
   fact, related fact, or unrelated* (`prompts/memory_reconcile.md`). The
   verdict logic is then deterministic code: same fact + newer event time →
   supersede (old row closed bi-temporally, `supersedes` chain, never
   deleted); same fact + older/unclear timing → NOOP or quarantine both for
   review; unrelated → ADD. This split is the single strongest result in the
   evaluation literature: LLM-resolved freshness scores 7–28% across
   production systems, LLM-match + deterministic-resolve scores 78–94.8%
   (03 §7).
4. **Instruction-kind candidates always quarantine.** A memory that changes
   future behavior is the poisoning attack surface (Microsoft's email-assistant
   case: 40→80% attack success via memorized instructions; 03 §7). Extracted
   or inferred instructions activate only through the HITL review queue.
   Instructions the user states explicitly through `memory.remember`
   (`source='user_stated'`) activate directly — the user said it, the store is
   visible — but carry provenance like everything else.

### 4.3 Read path — injection and tools

- **Pinned block** (Letta core-memory pattern, 02 §1): `pinned=true` rows are
  compiled into every injection surface under their own small budget — the
  standing user profile ("prefers markdown", "workspace = /workspace"). Pinning
  is a user/UI action, never automatic.
- **Retrieved block**: top-K active memories by the composite score —
  `w_rel·RRF(bm25, cosine) + w_rec·exp-decay(last_accessed) + w_imp·importance/10`
  (the GA triple over the 04 §2 SQL recipe; decay on *last access*, the
  rehearsal effect) — rendered as a compact "remembered context" block with
  ids, under the L0 budget. **Score floor**: below it, nothing injects — an
  empty block beats a distracting one (−67% from near-miss distractors, 03 §6).
  **Time-aware retrieval**: temporal phrases in the task expand into
  `valid_from/valid_to` filters (+7–11% temporal recall, 03 §6). Retrieval
  bumps access bookkeeping (batched).
- **In-loop tools** (registry citizens, hidden by default): `memory.recall
  (query, scope?, kinds?, as_of?)`, `memory.remember(text, kind, scope?)`,
  `memory.forget(memory_id)` (soft-retire; hard delete is UI/purge only). A
  seeded `memory-keeper` skill wraps them; exposure decides which loops may
  call them; §3.3 boundaries unchanged.
- **Abstention**: the injection block carries a fixed line ("if memory does
  not cover it, say so — never invent a remembered fact"); abstention is one
  of the two weakest abilities across all measured systems (03 §6) and gets
  its own eval probes.
- **Fencing**: memory renders as remembered *data*, never instructions
  (trust-boundary mitigation, 03 §7).

## 5. L3 — procedural learning (close the loop the registry leaves open)

Consumes the episodic layer; lands in existing governance. The
experience-following caution applies throughout: agents imitate retrieved
episodes nearly verbatim (r ≈ 1), so only positively-signaled experiences are
harvested, and pruning improves accuracy (03 §5).

1. **Routing stats** (`routing_stats`, consolidation-refreshed): per
   capability — asks handled, completion/deny/failure rates, mean tokens,
   latency, last used. Surfaced in registry UI ("learned" column); available
   to the planner behind `procedural_learning_enabled`.
2. **Plan exemplars** (`plan_exemplars`): successful graph-mode plans and
   agentic todo traces, keyed by task-text embedding, harvested only from
   positively-signaled runs (completed, no deny, no immediate correction).
   **ExpeL vote lifecycle** (03 §5): each exemplar carries a counter —
   upvoted when a run that used it succeeds, downvoted when one fails or is
   corrected; at zero it is retired. Planner prompt gains a budgeted top-2
   "similar past asks and what worked" block. **Measured goal**: cut the
   stage-30 finding-#4 fallback rate; the milestone's acceptance stage re-runs
   the stage-30 prompt suite and compares route distributions.
3. **Fallback mining → skill proposals**: cluster fallback-run digests
   periodically; a recurring cluster drafts a `.skill.md` via the existing
   template, runs doclint and the overlap judge, and lands as a **proposal**
   in the review queue. Human approval turns it into a normal dynamic skill
   (Voyager's additive library, 03 §5, behind the platform's own gates). No
   autonomous registry mutation — CoALA's point that procedural writes are the
   most dangerous kind (03 §1).

## 6. L4 — consolidation, reflection, decay (the ambient runway)

One in-process scheduler (lifespan asyncio task; `pg_try_advisory_lock(classid,
job_id)` per job class on a dedicated connection so exactly one replica works;
read/commit → LLM call outside any transaction → short write transaction with
optimistic guards — the 04 §5 discipline; §10 labels + Prometheus on every op;
failures log, never crash):

| Job | Trigger | What it does |
|---|---|---|
| digest + rollup | post-run | L1 digests (round-level), conversation rollups |
| extract + reconcile | post-run, debounced a few minutes after the conversation goes quiet (LangMem's ReflectionExecutor pattern, 02 §4 — reimplemented, not imported) | L2 write path (§4.2) |
| decay sweep | daily + idle | effective importance = `importance·exp(-Δt_access/half_life)` (MemoryBank's Ebbinghaus curve); below floor → `expired` (archived, not deleted); pinned immune. Deliberate forgetting measurably *improves* accuracy while shrinking the store 23–75% (03 §3) |
| reflection | importance-sum trigger — when the summed importance of recent unreflected memories crosses a threshold (the GA mechanism: sum>150 over ~100 recent events ≈ 2-3×/day) — plus idle | cluster → synthesize higher-order `inferred` memories **with explicit evidence citations to source memory ids** (auditable, GA-style); inferred instructions still quarantine |
| contradiction sweep | weekly | same-scope/kind/entity_key active pairs with high similarity → LLM same-fact check → deterministic resolution or quarantine (§4.2 rules); re-derive any rollup whose sources changed (consolidation-staleness, 03 §7) |
| routing stats + exemplar votes | idle + daily | L3 harvests and vote updates |
| embedding backfill | on `embedding_model` change + startup | side-table re-embed batches + `CREATE INDEX CONCURRENTLY` + flip (04 §6); ~$0.12 per 100k memories — cheap, don't over-engineer |

"Idle" = no active runs for `memory_idle_minutes` — the same detector ambient
mode will later use for watchers and standing intents. The evidence for doing
real work here is strong: sleep-time compute cuts test-time compute ~5× and
adds up to +13–18% accuracy on stateful tasks (03 §3). Every mutation fires the
existing NOTIFY discipline (≤8KB, ids only, cache-hint semantics with a
periodic reconcile sweep — 04 §5).

## 7. Governance, UI, privacy

- **Memory page** (admin UI): searchable/filterable list (scope/kind/status/
  source), edit-as-supersede (`source='user_edited'`), pin, hard-delete,
  review queue for quarantined rows (HITL card pattern), skill-proposal tab,
  provenance links into run traces, per-layer counters + last-consolidation
  status, and the MemBench axes on a small dashboard: effectiveness,
  op-latency, store size/degradation (03 §6).
- **Settings** (live-read, §3.7 style): `memory_enabled` (master, **default
  false** — §7.4 dark-rollout discipline), `memory_extraction_enabled`,
  `memory_reflection_enabled`, `procedural_learning_enabled`,
  `memory_injection_budget_tokens` (per surface), `memory_recall_top_k`,
  `memory_score_floor`, `memory_extraction_model`/params (nullable → default),
  per-kind half-life defaults, `memory_topics` allowlist,
  `memory_idle_minutes`.
- **Scope enforcement lives in SQL, not prompts**: scope columns filter every
  query; cross-scope reads escalate. Prompt-level protection collapses under
  context hijacking (94% → 45%) where architectural minimization holds 97%
  (AirGapAgent, 03 §7). Single-user POC, but the columns exist from day one.
- **Purge**: §8.7 purge extends to all memory tables — no residue.
- **No memory content in logs** — ids and counts only (§10 labels).

## 8. Failure modes → mitigations (evidence in 03)

| Failure | Measured severity | Mitigation here |
|---|---|---|
| Memory poisoning | ≥80% attack success at <0.1% poison rate (AgentPoison); >95% via query-only (MINJA); 40→80% via memorized email instructions (Microsoft) | strict admission gate; instruction quarantine + HITL; mandatory provenance; data-not-commands fencing; audit ledger |
| Retrieval distraction | up to −67% accuracy from related-but-wrong items; long-context 2.1% on unanswerable | per-surface budgets; score floor injects nothing when weak; abstention line + probes; top-K small |
| Stale preferences / failed updates | preference-following <10% after 10 turns; LLM freshness-resolution 7–28% | first-class preference rows; bi-temporal supersession; deterministic conflict resolution; decay |
| Experience-following / error propagation | imitation r ≈ 1; add-all 13% vs gated 38.5% | positive-signal-only harvesting; exemplar vote lifecycle; deletion sweeps |
| Context bloat | 30–60% drop at ~115K tokens (long context ≠ memory) | budgets; digests as retrieval units; expand-on-demand via `memory.recall` |
| Silent self-modification | CoALA: procedural writes are highest-risk | skill proposals through doclint + overlap judge + human approval only |
| Privacy leakage across scopes | prompt-level 94→45% under hijack; architectural 97% | SQL-enforced scopes; minimization; HITL escalation; visible store; purge |
| Cost creep | dedicated frameworks often lose to long-context baselines on accuracy — the honest justification is cost/latency (91% p95 reduction measured) | low-effort consolidation models; idle scheduling; per-job token metrics; **the eval suite measures tokens/latency, not just accuracy** |

## 9. Decisions on the table — and what the evidence settled

- **Native tables, not LangGraph `AsyncPostgresStore`** — settled by 04's
  source-verified findings: the store's semantic search does not use the HNSW
  index it builds (sequential scan by design, filter-mixing limitation); its
  flat `(prefix,key,value)` schema cannot express supersession, bi-temporality,
  importance, or composite scoring; and it brings a second migration authority
  that conflicts with "one Alembic migration per schema change." §7.0's
  OOB-first rule is honored where it bites: `AsyncPostgresSaver` stays for
  checkpoints, `SummarizationMiddleware` stays for compaction, no new custom
  middleware, and "custom" for the store is justified as *nothing OOB fits*.
- **No `langmem` dependency** — 0.0.30, ten months stale, and it hard-depends
  on `langchain-anthropic` + `langchain-openai` (04 §3), which would violate
  the §2.1 provider-isolation rule by construction. Its patterns (extract →
  search-similar → consolidate → upsert; debounced reflection) are
  reimplemented in ~200 lines with prompts in `app/prompts/`.
- **`StateClaudeMemoryMiddleware` / `FilesystemClaudeMemoryMiddleware`
  (langchain-anthropic)** — evaluated (02 §5): genuinely OOB middleware for
  file-style memory, but provider-scoped (Anthropic-only), so unusable as the
  core layer in a provider-agnostic platform. Noted as a future opt-in
  experiment behind the provider layer, nothing more.
- **pgvector image at M13 — recommended, needs sign-off** (it touches spec
  §7.3's "stock image" sentence): swap `postgres:16` →
  `pgvector/pgvector:0.8.6-pg16` (the official postgres image + extension,
  actively maintained; 04 §1). Same three services — an image pin change, not
  new infrastructure. This buys SQL-side hybrid retrieval (04 §2), HNSW on a
  grow-from-zero table, and the side-table dimension strategy. The
  conservative alternative — registry-style JSONB embeddings + in-process
  ranking — remains viable for the first weeks of data but hits a ceiling a
  personal agent will actually reach (tens of thousands of memories), and
  unlike the registries, memories grow without bound. Registries themselves
  stay JSONB, untouched.
- **A separate vector/graph DB** — never on the table (spec §2); the evidence
  agrees flat-plus-light-links is right at this scale (03 §4).
- **Full temporal knowledge graph** (Zep-style communities) — deferred with a
  written trigger (§4.1); the 7% conflict-resolution measurement (03 §7) says
  the graph is not where correctness comes from anyway.
- **Automatic behavior change** — nothing the system learns activates without
  user visibility (facts/preferences) or explicit approval (instructions,
  skills).

## 10. Rollout shape

Five milestones (detailed in 06): substrate → episodic → semantic → procedural
→ reflection/evals. Every milestone: tests first, dark by default, one
acceptance stage with UI evidence, `memory_enabled=false` byte-identical to
today (the M11 discipline, verified by running the existing suites against a
memory-enabled build with the flag off). The eval harness measures the
LongMemEval five abilities *and* token/latency deltas — because the honest
case for memory over long-context is precision plus cost, and both claims get
numbers (03 §6).
