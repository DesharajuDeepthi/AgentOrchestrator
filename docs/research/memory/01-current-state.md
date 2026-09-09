# Memory research 01 — what this codebase already has

Everything below is verified against the code on `memory_xperiment` (branched from
main at `a732043`, post-PR #13). The point of this inventory: the platform already
owns more memory machinery than a greenfield project would — the memory layers
should *promote and connect* these pieces, not duplicate them.

## The four memory types, as they exist today

Using the CoALA vocabulary (working / episodic / semantic / procedural):

### Working memory — partial, run-scoped, already OOB-compacted

- **History window**: `build_history()` (`orchestrator/graph_mode.py`) reconstructs
  conversation history from **runs** — there is no `messages` table; a turn is
  `runs.chat_message` + `runs.final_answer`. The window is hard-capped: last 20
  completed runs, last 8 000 characters. The planner sees this string; the agentic
  orchestrator gets the same via `build_history_messages()`.
- **Compaction**: `SummarizationMiddleware` (LangChain OOB) rides every
  `create_agent` instance — skill loops, rung-1, agentic orchestrator — per the
  §7.0 stack builder. Long in-loop contexts already self-compact.
- **Run state**: LangGraph Postgres checkpointer (`langgraph-checkpoint-postgres`,
  already a dependency, wired in `db.py`) persists graph state for HITL
  pause/resume. Thread-scoped state survives process restarts.
- **Direct-invocation context**: M11's opt-in history summary — one low-effort
  summarization call over the same capped window, recorded as a `summary` run
  step, prompt in `prompts/history_summary.md`.

**Gap**: the window is recency-only (last 20 runs). Anything older is invisible to
every surface. No salience, no retrieval, no cross-conversation reach.

### Episodic memory — the strongest existing asset, currently write-only

- **Runs ledger** (`models/run.py`): every run stores the verbatim ask, the plan
  JSON, a **frozen capability snapshot** (`runs.snapshot` — the resolved
  persona/workflow/model/tool definitions at dispatch time, immune to later
  registry edits), final answer, formatter artifact, charts, error, token totals,
  timestamps, mode.
- **Run steps**: typed steps (`plan|route|skill|hitl|tool_call|aggregate|summary`)
  with input/output JSONB, per-step model + tokens + duration + error, and
  `parent_step_id` nesting. Route steps record the §7.1 **rung** and what it
  resolved to. HITL steps record `{status: approved|denied, note}` — the user's
  actual words at decision points.
- **Observability correlation**: every span/log/metric carries the §10 label set
  keyed by `run_id`/`step_id` — episodes are cross-referenced in three systems.

**Gap**: nothing ever *reads* this ledger back except the UI. No outcome
annotations (was the answer good?), no rollups, no retrieval. It is a perfect
episodic store that no cognitive process consumes.

### Semantic memory — absent

Nothing stores durable facts about the user, their preferences, entities they
care about, or standing instructions. Every conversation starts from zero beyond
the 20-run window. HITL notes ("not yet — I will circulate it manually"), format
choices (markdown vs plain-text at form gates), and repeated corrections are
generated and then lost. This is the biggest missing layer.

### Procedural memory — unusually strong, static-only

- The **registry itself is procedural memory**: tools (atomic actions), skills
  (persona + instructions + tool boundary), sub agents (workflow DAGs over
  skills). It is embedded (M7), retrieved (§7.4), exposure-gated, cached with
  event invalidation, and seeded from declarative `.skill.md`/`.agent.md` files
  with a lint gate (doclint).
- **Prompts as files** (`app/prompts/*.md`) — the fixed procedural knowledge.

**Gap**: procedures only change when a human edits the registry. The system never
learns from its own runs — no routing statistics, no exemplar plans, no
"the fallback keeps doing X by hand, that should become a skill" loop. Stage-30
finding #4 (indirect asks fall to the fallback far too often) is exactly the
symptom of missing procedural learning.

## Infrastructure the memory layers can stand on

| Piece | Where | Why it matters for memory |
|---|---|---|
| Embeddings port | `llm/` — `get_embeddings("provider:model", texts)`, `supports_embeddings()` | provider-agnostic memory embeddings for free; degrades to lexical-only when unset (`embedding_model` setting, nullable) |
| Hybrid ranker | `retrieval.py` — BM25 + cosine + RRF (k=60), pinned-id bypass, drop-count logging, memoized query vectors | the exact scoring machinery memory retrieval needs, already conventions-compliant; extend with recency/importance terms |
| Embedding storage pattern | `embedding jsonb` + `embedding_hash` on registry rows; best-effort write-path + startup backfill | stock `postgres:16` image kept deliberately (spec §7.3 names pgvector as the documented swap when catalogs outgrow in-memory ranking) — registries are small and stay this way; memories grow without bound, so 04/05 recommend taking the documented swap at the memory milestone |
| Cache + invalidation bus | `registry_cache.py` — bypass/memory/redis modes, generation counters, reload-on-dirty, `pg_notify` cross-replica with origin filtering, TTLs forbidden | a memory-store cache follows the identical pattern; the NOTIFY channel discipline already exists |
| Background work | single asyncio process; MCP health monitor loop as precedent | consolidation jobs = asyncio tasks in lifespan + pg advisory locks; no broker allowed (spec §2) and none needed |
| HITL plumbing | approve/deny + form gates + notes, idempotent resume | write-gates for high-stakes memory (standing instructions), review queues for quarantined memories |
| Settings store | live-read `app_settings`, validated PATCH, no restart | per-layer toggles/budgets/decay knobs, dark-by-default rollout like §7.4 (`retrieval_enabled: false`) |
| Overlap judge | `overlap.py` + `prompts/overlap_judge.md` — LLM judge on skill/sub-agent save | dedup gate when procedural learning proposes new skills |
| Seed documents + doclint | `.skill.md` / `.agent.md` + build-gated lint | learned procedures can graduate to reviewable files, same format, same gate |
| Checkpointer | LangGraph AsyncPostgresSaver | thread-scoped state is handled; memory = the cross-thread store side |
| Obs labels | §10 label set on every step | memory ops must emit the same labels (`tier`, new `kind` values) |

## Constraints that bind the design (from spec + CLAUDE.md)

1. **§2 — no new infrastructure**: three compose services; Redis optional
   cache-only; truth in Postgres. Memory storage must be Postgres tables
   (stock image today; pgvector is a *documented swap*, not a default).
2. **§2.1 — provider layer is law**: all embedding/LLM calls through
   `get_model`/`get_embeddings`; structured outputs via LangChain abstractions.
3. **§7.0 — middleware precedence**: OOB LangChain first; compose/subclass
   second; the only sanctioned custom middlewares are the three registry
   projections. A new memory middleware therefore **requires a spec amendment**
   — or memory rides existing surfaces (prompt assembly, tools, store).
4. **All prompts in `app/prompts/`** — extraction/consolidation/reflection
   prompts are files.
5. **§4 — registry ids immutable; static definitions frozen** — memory records
   are a new table family, not registry rows; but *procedural* learnings can
   graduate into dynamic registry entries through the normal creation paths.
6. **§10 — labels on everything**; **§13 — mypy strict, Alembic migration per
   schema change, conventional commits, tests-first per milestone**.
7. **Single-user POC** — no `user_id` anywhere yet. Memory scopes must still be
   designed (global/project/conversation) so multi-user does not require a
   rewrite, but the POC ships single-tenant.

## Existing behaviors the memory layers must not break

- History flag semantics (M11): off = byte-identical cold behavior; HITL resume
  never re-summarizes.
- Formatter/answer arrangement is run-time-frozen — memory injection must not
  retroactively change how past runs render.
- Registry cache contract: a cache entry is current or explicitly invalidated —
  the same discipline applies to any memory cache.
- The rung-4 exposure gate (stage 29): hidden skills never compose into workers.
  Memory tools, if registered as native tools, inherit exactly this governance —
  which is a feature, not a constraint: **memory operations become registry
  citizens**, exposable, hideable, and HITL-gateable like everything else.
