# 07 — Experiment results: memory-layer ablation on the live stack

**Date:** 2026-08-20 · **Branch:** `memory_xperiment` · **Harness:** `experiments/memory/`

This is the measured comparison the branch was named for: the finished M13–M17
memory subsystem, run layer-by-layer against the existing solution (memory
dark), on the live stack, with real model calls and deterministic grading.

## 1. Method

- **Live stack, real runs.** Every probe turn is a real `POST /chat` against the
  running compose stack (graph orchestrator; default model
  `anthropic:claude-sonnet-4-6`, planner role overridden to
  `anthropic:claude-sonnet-5` at high effort — a stage-20 setting that was
  identical across all five configs, and the planner's `direct_answer` is what
  answers these probes). Conversation slots map to real conversations,
  so cross-conversation recall is genuinely cross-conversation — nothing is
  simulated or short-circuited.
- **Probe suite** (`experiments/memory/probes.py`): 7 probes over the six
  LongMemEval ability categories (research 03 §6) — information extraction ×2,
  multi-session reasoning, knowledge update, temporal reasoning, abstention,
  preference following. Grading is fully deterministic (required/forbidden
  substrings, bullet counts) — no LLM judges, so reruns are comparable.
- **Configs** (`experiments/memory/harness.py`): each config purges memories +
  runs, resets settings, then runs the whole suite fresh:

  | config | layers | settings delta |
  |---|---|---|
  | `off` | none — the existing solution | `memory_enabled=false` |
  | `episodic` | L0/L1 digests + rollups | extraction off, procedural off |
  | `semantic` | + L2 extraction/reconciliation | extraction on |
  | `full` | + L3 procedural exemplars/stats | procedural on |
  | `tight` | full, starved | injection budget 1200→250 tokens, score floor 0.35→0.6 |

- **Embeddings:** the deterministic fake provider (`fake:scripted`, 64-dim
  bag-of-tokens) drives the vector leg of hybrid recall, because the stack's
  only configured key is Anthropic and Anthropic has no embeddings API. This
  is a **conservative floor**: real embedding models separate paraphrases far
  better than bag-of-tokens cosine, so vector-recall quality can only improve
  in production.
- **Metrics:** pass/fail per probe, input/output tokens on question runs
  (`usage_metadata` totals from the runs ledger), setup-turn tokens separately,
  wall-clock latency per question.

## 2. Headline matrix

| config | score | question input tok | question output tok | setup input tok | mean latency | active memories |
|---|---|---|---|---|---|---|
| **off** (existing solution) | 2/7 | 109,479 | 8,966 | 31,620 | 24.9 s | 0 |
| **episodic** | 6/7 | 54,419 | 3,494 | 33,369 | 9.3 s | 0 |
| **semantic** | **7/7** | **39,851** | **2,187** | 33,607 | **7.1 s** | 7 |
| **full** | 7/7 | 56,664 | 3,135 | 49,318 | 9.0 s | 6 |
| **tight** | 7/7 | 76,863 | 6,986 | 33,603 | 17.7 s | 6 |

Per ability:

| ability | off | episodic | semantic | full | tight |
|---|---|---|---|---|---|
| information extraction | 0/2 | 1/2 | 2/2 | 2/2 | 2/2 |
| multi-session reasoning | 0/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| knowledge update | 0/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| temporal ("before the change") | 0/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| abstention | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| preference following | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |

## 3. Findings

### F1 — Memory improves accuracy *and* cost at the same time

The baseline doesn't just fail 5/7 probes — it burns **2.7× the input tokens**
(109k vs 40k) and **3.5× the latency** (24.9s vs 7.1s) failing them. Without
memory, the orchestrator answers "what's my dog's name?" by searching the
workspace, listing files, and trying tools before giving up; with memory the
injected block answers it in one pass. Memory is not a tax on the run path —
on this workload it is a *discount*.

### F2 — Semantic (L1+L2) is the sweet spot

`semantic` is the only config on the Pareto frontier for all three axes:
perfect score, fewest tokens, lowest latency. Extraction distills setup turns
into compact facts ("dog is named Biscuit", `entity_key=deploy_branch →
release-2026`), which inject smaller and rank better than raw episodic
digests. It also passed the temporal probe: the reconciliation pipeline's
supersession chain kept the pre-correction value (`main`) answerable — the
ability LongMemEval calls the field's weakest.

### F3 — Episodic alone is strong but has a first-touch gap

`episodic` (digests only, no extraction) scored 6/7 at half the baseline's
cost. Its one failure was `single_fact_recall` — the *first* question of the
suite, asked immediately after the facts were stated in another conversation.
Digest-only recall lost the race/ranking on first touch, yet passed
`second_fact_recall` over the same digest minutes later. Semantic extraction
eliminates this gap (facts land as ranked, compact memories immediately).

### F4 — Procedural learning is cost-neutral-to-negative on knowledge probes — as designed

`full` kept the 7/7 score but spent ~42% more question tokens than `semantic`
(56.7k vs 39.9k) and ~47% more setup tokens (exemplar harvesting runs after
every setup turn). Plan exemplars target *routing* quality, and none of these
probes exercise routing — so on this suite L3 is overhead. Its measured goal
(fallback-rate reduction on indirect asks) is tested separately in §4.
Tweaking conclusion: **enable L3 only if your workload routes to
skills/sub-agents; leave it off for pure Q&A assistants.**

### F5 — Starving the injection budget doesn't break accuracy, it moves the cost

`tight` (budget 250 tokens, floor 0.6) still scored 7/7 — but at ~2× the
tokens and ~2.5× the latency of `semantic`. When injection is starved, the
model compensates through the `memory.recall` tool and extra exploration:
correctness survives (the tools are registry citizens precisely for this),
but every compensating recall is a full round trip. Tweaking conclusion:
**a generous injection budget is net cheaper than a starved one** — the
1200-token default beats 250 on every axis; cutting budget is a false economy.

### F6 — Abstention held at every layer (with one grader lesson)

No config transplanted the dog's name onto the cat — the data-fenced block
plus the abstention line ("If the memories don't cover it, say so") held even
with 7 active memories injected. An earlier grader draft forbade the substring
"biscuit" outright and flagged the correct answer "I only have information
about your dog, Biscuit" — the forbid rule now targets actual transplantation
("cat's name is biscuit"), which is the honest failure mode.

## 4. M16 measured goal — fallback rate with procedural learning

**Design** (`experiments/memory/fallback_experiment.py`): warm plan exemplars
with two successful direct-phrased asks that route to sub agents
(site-analyst → `custom_sub_agent`, workspace-warden → `native_sub_agent`),
then send indirect paraphrases — the stage-30 shape that fell back — with
procedural learning OFF vs ON and compare route rungs. Extraction stays off
to isolate the L3 effect.

**Result: the effect was not measurable, for two instructive reasons.**

| round | asks | fallback OFF | fallback ON |
|---|---|---|---|
| 1 | indirect, some capability words ("site notes", "workspace") | 0/3 | 0/3 |
| 2 | oblique, zero capability words ("line 3 walkthrough", "field visit") | 0/3 | 0/3 |

- **Round 1:** the router never failed. The `claude-sonnet-5` planner over the seeded
  registry descriptions routed every indirect paraphrase to the right sub
  agent without exemplar help. The stage-30 fallback finding did not
  reproduce under these conditions — the control arm has nothing for L3 to
  fix.
- **Round 2 (the interesting one):** the two obliquest asks produced
  `rungs=[]` in *both* arms — **no routing happened at all**. The planner
  answered directly from the injected episodic digests of the warm-up runs,
  citing memory ids and reproducing the specifics correctly (vibration
  2.1 mm/s, part SC-118, pending PLC firmware). The asks designed to confuse
  the router never reached the router: **L1 episodic memory absorbed the
  re-asks upstream of L3's target problem.** Fallback-prone indirect asks
  are typically *re*-asks about prior work — and prior work is exactly what
  digests cover.

**Conclusion:** the L3 exemplar mechanism is implemented and exercised
(both warm-ups harvested exemplars; recall, voting, and fallback→mined-skill
clustering are covered by the unit suite), but on this catalog its
end-to-end effect is masked — first by a router that doesn't fail on
paraphrases, then by episodic injection answering repeat asks before routing
begins. This *strengthens* the F4 recommendation: procedural learning is a
targeted layer for catalogs/models where routing demonstrably fails, not a
default-on layer. The layer to reach for first is episodic+semantic — it
removed the failure class this experiment went looking for.

## 5. Defects found *by* the experiment (fixed on this branch)

The experiment loop earned its keep beyond the matrix — three real defects
surfaced only under live measurement:

1. **`websearch_to_tsquery` AND-semantics vetoed recall** (`or_tsquery` fix,
   `f2a15a2`). Question boilerplate ("One short sentence") ANDed into the
   lexical query, so a stored fact matching only the content words scored
   zero — and the failed question's *own* digest outranked the truth on the
   next attempt. Lexical legs now build an OR-joined sanitized tsquery.
   Episodic went 3/7 → 6/7 on this fix alone.
2. **Memory purge 500ed on supersession chains** (`ed03297`). The
   self-referential `supersedes`/`superseded_by` FKs blocked the purge's
   two-pass delete the moment any fact had been corrected. Purge now nulls
   both link columns first; regression test covers a three-link chain.
3. **Silent purge failures contaminated configs** (`ed03297`). The harness
   POSTed to a route that didn't accept POST (405) and ignored response
   codes, so each config inherited the previous config's memories. A
   contaminated `full` run scored 4/7 — a leftover `main` fact from the
   prior phase survived reconciliation and beat the correction. Resets now
   raise on any non-2xx and verify the store is empty; the clean rerun
   scored 7/7. (Kept as a cautionary note: **cross-config contamination
   reads as "the new layer made things worse" when it's actually stale
   state.**)

## 6. Caveats

- **Fake embeddings floor.** All memory-on numbers used 64-dim bag-of-tokens
  vectors. Real embeddings should improve paraphrase recall (and could only
  have helped `episodic`'s one failure).
- **Preference probe is non-discriminative.** The baseline also passed it —
  bullet-formatted answers are common unprompted. It stays in the suite for
  regression value, not as evidence of memory benefit.
- **Small N.** 7 probes × 5 configs is an ablation smoke test, not a
  benchmark. Directional conclusions (F1–F5) rest on large effect sizes;
  fine-grained ranking between 7/7 configs rests on cost, not score.
- **One model pairing.** All runs on `claude-sonnet-5` (planner) over `claude-sonnet-4-6` (other roles). The §15 eval roadmap
  (spreadsheet-driven, LangSmith-published) is the vehicle for multi-model
  sweeps.

## 7. M18 — closed-loop refinement (implemented and measured)

The post-M17 enhancement review was implemented as spec §16.7 (commit `b2f0852`),
sharpening the winning layers rather than adding new ones:

- **Citation feedback (used beats retrieved).** Injection no longer bumps
  access bookkeeping; a post-run job matches injected memory-id prefixes
  against the final answer and reinforces only cited memories (+1 importance,
  capped; access bump). Uncited injections cool toward decay. Explicit
  `memory.recall` tool calls still count as use.
- **Digest compaction.** Run-digests older than 14 days (setting) fold into
  one period digest per conversation; raw rows and embeddings are deleted.
  The episodic store — the one unbounded table in the M17 design — becomes
  O(conversations).
- **Entity-hop recall.** Extraction names 0–3 entities per memory; writes
  maintain `memory_entities`/`memory_entity_links`; recall appends up to two
  floor-exempt linked memories at a discount of the weakest direct hit
  (skipped for kind-filtered and point-in-time recalls). One bounded join —
  no graph database.
- **Two "free" items verified rather than built**: OpenAI/Google embedding
  adapters already implement the provider port (production = set a key +
  `embedding_model`), and `memory.recall` already exposed `as_of` — both now
  covered by tests.

**Long-horizon time-warp simulation** (`experiments/memory/longhorizon.py`,
deterministic, no LLM): a 90-day backdated store — four memory cohorts, 8
conversations × 11 runs of digests, a seeded entity_key contradiction — run
through decay, contradiction, and compaction. All six equilibrium checks pass:

| check | result |
|---|---|
| E1 untouched low-importance memories expire | ✅ 10/10 expired |
| E2 rehearsed low-importance memories survive | ✅ 10/10 active |
| E3 pinned rows immune regardless of age | ✅ 3/3 active |
| E4 high-importance rows outlive same-age peers | ✅ 10/10 active |
| E5 episodic store compacts to O(conversations) | ✅ 88 digests → 7 period + 11 recent |
| E6 duplicate active entity_keys quarantined, oldest-validity wins | ✅ |

**Live regression (three rounds of the full probe suite on the M18 build,
real models):** accuracy held at **7/7 in every round**, and the re-runs
earned their cost by surfacing one more real defect. In rounds 1–2 the
`knowledge_update` probe reproducibly cost ~38k input tokens (vs ~6k
pre-M18): the extractor had parroted an injected-block citation into memory
text and phrased the fact as a change event — "The deploy branch was changed
from `main` to `release-2026` (episode 1fcf6bb1)" — which superseded the
clean current-state fact; the planner then distrusted the event-shaped
memory and re-verified via full-catalog fallback. Fixed deterministically
(`e63e2ea`): the admission gate strips citation-id markers, and the
extraction prompt requires current-state phrasing. Round 3 confirmed
`knowledge_update` back to 5.8k tokens direct. Residual cost spikes moved to
different probes each round (multi-session in one, abstention in another) —
stochastic sonnet-5 self-verification at effort high, present with or
without memory, and it always produced correct answers. Memory content,
scores, injection, citation feedback, and entity links were verified correct
in the traces each round.

## 8. Recommended default configuration

Based on the matrix: **`semantic` shape** — memory on, extraction on,
procedural off (until the workload routes), injection budget 1200, floor
0.35, top-k 6, `fake:scripted` replaced by a real embedding model in
production. This is the configuration that dominated every axis measured.


## 9. Cross-provider retest (M19) — the port proves out

An `openrouter` gateway adapter (spec §2.1's "custom gateway" scenario) was
added with zero consumer changes, and the identical battery — routing
experiment + memory off/semantic probe suite, real runs, deterministic
grading — ran on five OpenRouter models against the Claude baseline. Raw
results: `result_*_{glm,kimi2,qwen38max,oxalpha2,qwen36plus}.json`.

| model (all roles) | memory off | memory semantic | semantic tokens / latency | indirect-ask fallbacks (L3 off→on) | notes |
|---|---|---|---|---|---|
| claude (sonnet-5 planner / sonnet-4-6) | 2/7 | **7/7** | 82k / 16.4s | 0/3 → 0/3 | reference |
| z-ai/glm-5.3 | 2/7 | 5/7 | 27k / 60.1s | routing OK; run never settled ≤600s | formatter A2UI call takes minutes; workspace-warden run >10min |
| moonshotai/kimi-k3 | 2/7 | 5/7 | 35k / 29.2s | 0/3 → 0/3 (round 1) | intermittent planner structured-output schema failures; slow runs can exceed 600s |
| qwen/qwen3.8-max | 2/7 | **7/7** | 31k / 25.1s | 0/3 → 0/3 | matches Claude on the suite at ~⅓ the tokens |
| stealth/ox-alpha | 2/7 | **7/7** | 30k / 21.3s | 0/3 → 0/3 | first round fully upstream-rate-limited (shared pool); retry clean |
| qwen/qwen3.6-plus | 2/7 | 5/7 | 44k / 24.7s | **1/3 → 0/3** | first observed L3 win: procedural learning removed a genuine fallback |

Cross-provider findings:

1. **The architecture is provider-portable.** Every model produced the same
   *shape*: memory off = 2/7 (only abstention + preference), memory on =
   large lift. The platform machinery — routing ladder, HITL, digests,
   extraction, injection, citation feedback — ran unmodified everywhere.
2. **The two recurring semantic misses are model-quality, not machinery.**
   GLM/Kimi/Qwen-3.6 fail `single_fact_recall` (first-touch race — their slow
   extraction hasn't landed when the first question arrives) and GLM/Qwen-3.6
   fail `knowledge_update` the same way: their reconciliation judge leaves the
   stale fact active and recall serves it. The LLM-judged step inherits the
   judge's quality; the deterministic paths (gate, supersession, decay) never
   differed across providers.
3. **Adapter quirks stay in the adapter** (spec §2.1 vindicated): GLM ignores
   `response_format` json-schema → structured output defaults to function
   calling; Z.AI rejects forced tool_choice → coerced to auto. No consumer
   changed.
4. **Gateway latency is the real tax.** Claude settles probe runs in seconds;
   GLM's A2UI formatter call alone can take minutes, and Kimi intermittently
   exceeded a 600-second run ceiling. For gateway models, `formatter_enabled=false`
   or a fast formatter-role override is the practical configuration.
5. **L3's first measured win appeared on the weakest router** (qwen3.6-plus),
   consistent with §4's conclusion: procedural learning pays off exactly when
   the planner actually fails — strong planners never gave it the chance.
