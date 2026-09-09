# Memory research 03 — academic landscape & evaluation

> Researched 2026-08-20 by a dedicated research agent; claims verified against
> papers/pages that session unless marked *[training knowledge]*. Companion
> docs: 01 (current state), 02 (production), 04 (substrate), 05 (proposal),
> 06 (spec amendment).

Aimed at: a self-hosted concierge agent (FastAPI + Postgres + LangChain/LangGraph,
single asyncio process, tool/skill/sub-agent registry, HITL gates, full runs
ledger, planned idle-time "ambient mode").

---

## 1. CoALA: the standard vocabulary

**Cognitive Architectures for Language Agents** (Sumers, Yao, Narasimhan, Griffiths — [arXiv:2309.02427](https://arxiv.org/abs/2309.02427), TMLR 2024) imported 40 years of cognitive-architecture practice (SOAR, ACT-R) into LLM-agent design. Its core move: describe any agent as (a) **memory modules**, (b) an **action space** split into internal and external actions, (c) a **decision cycle**.

| CoALA store | Contents | Concierge-platform analogue |
|---|---|---|
| **Working** | Current turn's context: goals, intermediate reasoning, retrieved items | LangGraph state / message window per run |
| **Episodic** | Records of specific past experiences (trajectories, conversations, outcomes) | The runs ledger — already exists |
| **Semantic** | Facts about the world and the user | A `memories` table in Postgres |
| **Procedural** | How-to knowledge: code, skills, prompts, the agent's own machinery | The tool/skill/sub-agent registry |

Actions split into **grounding** (external), and internal **retrieval**, **reasoning**, and **learning** (working → long-term writes). Two observations that matter for design: learning is not one thing — writing an episode, updating a fact, and adding a skill are *different actions with different risk profiles* — and procedural-memory writes are the most dangerous self-modification (a bad skill corrupts all future behavior → HITL-gate registry writes). *[Training knowledge: LangGraph's own long-term-memory docs adopt the semantic/episodic/procedural split, citing CoALA.]*

---

## 2. Generative Agents (Park et al., 2023): retrieval scoring and reflection

**Generative Agents** ([arXiv:2304.03442](https://arxiv.org/abs/2304.03442), UIST 2023): 25 agents in a sandbox town, each with a **memory stream** — append-only, timestamped (creation + last-access) natural-language observations.

**Retrieval scoring (the actual math).** `score = α_rec·recency + α_imp·importance + α_rel·relevance`, all α = 1, each component min–max normalized to [0,1]:
- **Recency**: exponential decay on *hours since last access* (not creation), factor **0.995/hour** (≈5.8-day half-life) — re-accessing a memory refreshes it (built-in rehearsal).
- **Importance**: LLM-assigned integer **1–10** at write time.
- **Relevance**: cosine similarity to the query.

This additive three-factor score — cheap to compute in SQL over a vector-similarity candidate set — is the single most-copied design in the field.

**Reflection trees.** When the **sum of importance over the ~100 most recent events exceeds a threshold (150)** — ~2–3×/day in the sim — the agent (1) asks the LLM for the 3 most salient high-level questions given recent memories, (2) uses them as retrieval queries, (3) synthesizes ~5 **insights with explicit citations to the evidence memories**, (4) writes the insights back. Reflections can reflect on reflections → a **tree whose leaves are raw observations**. The evidence-pointer detail makes synthesized memory *auditable* — mapping directly onto the runs ledger.

**Evaluation.** Ablation (TrueSkill, human-ranked interviews): full architecture **μ = 29.89** > no-reflection **26.88** > no-reflection-no-planning **25.64** > human-authored condition **22.95**. **Replications:** the architecture replicated and generalized (open-source release, AI Town *[training knowledge]*; formula absorbed into LangChain's `GenerativeAgentMemory` and AWS AgentCore guidance). Strongest follow-up: **"Generative Agent Simulations of 1,000 People"** ([arXiv:2411.10109](https://arxiv.org/abs/2411.10109)) — interview-grounded agents replicated real individuals' General Social Survey answers **85% as accurately as the individuals replicated themselves two weeks later**. Caveats: token-expensive; LLM importance scoring is noisy — replications keep the *three-factor structure* even when swapping components.

---

## 3. Forgetting and consolidation

**MemoryBank** ([arXiv:2305.10250](https://arxiv.org/abs/2305.10250), AAAI 2024) — the canonical "agents should forget" paper. **Ebbinghaus forgetting curve**: retention `R = e^(−t/S)`; **each recall increments strength S and resets t** — used memories persist, unused ones become deletable. Also pioneered hierarchical consolidation: turn logs → daily summaries → global summaries → an evolving **user-personality portrait** (the three-tier shape production systems now use). The authors call the decay rule "exploratory"; the durable ideas are *access-driven decay* and *summarize-then-forget*.

**Deliberate deletion works:** the experience-following study ([arXiv:2505.16067](https://arxiv.org/abs/2505.16067)) found history-based deletion **improved** accuracy while shrinking the store ~23%; combined policies cut memory ~75% without collapse.

**RAPTOR** ([arXiv:2401.18059](https://arxiv.org/abs/2401.18059), ICLR 2024) — consolidation for retrieval: recursively embed → cluster → summarize into a **tree of abstractions**, retrieve from *all* levels. +20% absolute on QuALITY with GPT-4. The pattern for summarizing long runs/conversations into a queryable hierarchy — important because LoCoMo found summary-only retrieval *loses* information vs fact-level retrieval (§6).

**MemGPT** ([arXiv:2310.08560](https://arxiv.org/abs/2310.08560)) *[details from training]* — the OS analogy: small main context + paged external storage, agent **self-edits memory via function calls** — memory management as tool-calls inside the loop.

**Sleep-time / offline consolidation.** **"Sleep-time Compute"** ([arXiv:2504.13171](https://arxiv.org/abs/2504.13171)): think offline about stored context before queries arrive, pre-computing inferences into rewritten context. **~5× less test-time compute** for equal accuracy; **up to +13%/+18% accuracy**; amortization cuts per-query cost **2.5×**; gains track query predictability. With Letta's [sleep-time agents blog](https://www.letta.com/blog/sleep-time-compute/), this is the direct academic blueprint for ambient mode: reflection, MemoryBank summarization, RAPTOR trees, and contradiction sweeps are all sleep-time jobs on the runs ledger.

---

## 4. Graph-structured memory

**HippoRAG** ([arXiv:2405.14831](https://arxiv.org/abs/2405.14831), NeurIPS 2024) — hippocampal indexing: LLM builds a schema-less open-IE KG over passages; retrieval runs **Personalized PageRank seeded on query entities** → multi-hop association in one pass. Up to **+20%** over SOTA RAG on multi-hop QA; matches/beats iterative retrieval while **10–30× cheaper, 6–13× faster**. **HippoRAG 2** ([arXiv:2502.14802](https://arxiv.org/abs/2502.14802)) adds passage nodes + LLM triple-filtering; **+7% associative memory** over the best embedding retriever *without* losing simple recall.

**A-MEM** ([arXiv:2502.12110](https://arxiv.org/abs/2502.12110)) — **Zettelkasten as agent memory**: each memory becomes a structured note (context, keywords, tags); the system *agentically decides links*; new notes trigger **memory evolution** (updates to existing notes' contexts). On LoCoMo: roughly **2× multi-hop QA** (GPT-4o-mini: **45.85 F1 vs MemGPT 25.52**) at **85–93% lower token cost per op**. Transferable idea: linking and link-time enrichment are *write-time investments that pay off at multi-hop read time*.

**GraphRAG** ([arXiv:2404.16130](https://arxiv.org/abs/2404.16130)) — global sense-making: entity graph → Leiden **communities** → pre-generated community summaries → map-reduce answers. **72–83% comprehensiveness / 62–82% diversity win rates** vs naive vector RAG; root-level summaries use **>97% fewer context tokens**.

**When graphs win — and don't.** Graphs win on **association across records** and **corpus-global synthesis**. They do *not* reliably win single-hop recall: Mem0's own graph variant gained only ~2% overall on LoCoMo ([arXiv:2504.19413](https://arxiv.org/abs/2504.19413)); "Structural Memory" ([arXiv:2412.15266](https://arxiv.org/abs/2412.15266)) found **no single structure dominates** — mixed structures are the robust choice; and the freshness study (§7) measured a temporal-KG system at **7%** on conflict resolution. Verdict: flat Postgres vector store as substrate, *lightweight* graph structure (typed links, entity tags) for multi-hop — not a graph database.

---

## 5. Self-improvement memory: storing "what worked"

**Reflexion** ([arXiv:2303.11366](https://arxiv.org/abs/2303.11366), NeurIPS 2023) — verbal RL: after a failed episode, convert feedback into a **natural-language lesson** in a small buffer, replayed on retry. **91% pass@1 HumanEval vs GPT-4's 80%**; **+22 pts ALFWorld**; **+20% HotPotQA**. Limitation: lessons are *task-local* — cross-task value needs ExpeL-style distillation.

**Voyager** ([arXiv:2305.16291](https://arxiv.org/abs/2305.16291)) — the procedural-memory landmark: an **ever-growing library of executable, self-verified code skills**, indexed by embedded descriptions, retrieved by task similarity, composing older skills. **3.3× more unique items, 2.3× longer traversal, milestones up to 15.3× faster**; the library **transfers to a brand-new world**; additive writes avoid catastrophic forgetting. This is the registry's growth story: skills as versioned artifacts, embedding-retrieved, HITL-gated on write.

**ExpeL** ([arXiv:2308.10144](https://arxiv.org/abs/2308.10144), AAAI 2024) — **experience distillation**: extract cross-task **insights from success/failure comparisons**, maintained with explicit **ADD / EDIT / UPVOTE / DOWNVOTE** ops (importance counter; delete at zero). HotpotQA **39% vs ReAct 28%**; ALFWorld **59% vs 40%**; insights *transfer* across tasks (HotpotQA→FEVER 70% vs 63%). The cleanest published lifecycle for a self-correcting "lessons learned" store.

**The caution — experience-following** ([arXiv:2505.16067](https://arxiv.org/abs/2505.16067)): with populated memory, input-similarity and output-similarity correlate near-perfectly (r ≈ 1) — the agent *imitates* retrieved episodes; bad episodes reproduce (**error propagation**), near-miss episodes misguide (**misaligned replay**). **Write-gating dominated everything**: strict admission **38.50%** vs **13.05% add-all** on EHRAgent — a ~3× swing from write policy alone.

---

## 6. Benchmarks and what they reveal

**LOCOMO** ([arXiv:2402.17753](https://arxiv.org/abs/2402.17753), ACL 2024): 300-turn, up-to-35-session dialogues; QA incl. **adversarial-unanswerable**. Findings: best model 37.8% F1 vs **human 87.9%**; **temporal reasoning 20.3% vs human 92.6%**; RAG over **atomic observations beat session-summary retrieval 41.4 vs 29.9 F1** (summaries lose information); long-context models scored **2.1% on adversarial questions** — long contexts *induce* confident fabrication. LOCOMO is also the de-facto vendor leaderboard — treat single-system claims as marketing (see 02 §2's Mem0/Zep dispute).

**LongMemEval** ([arXiv:2410.10813](https://arxiv.org/abs/2410.10813), ICLR 2025): 500 questions in scalable fake chat histories testing **five abilities: information extraction, multi-session reasoning, temporal reasoning, knowledge updates, abstention**. Headline: commercial assistants and long-context LLMs drop **30%** (30–60% at ~115K tokens) when answers must be *recalled* — **long context is not memory**. Actionable design results: **round-level indexing beats session-level and summary-only**; **fact-extraction as index keys** +4% recall/+5% accuracy; **time-aware indexing + temporal query expansion** +7–11% temporal recall; structured reading up to +10 pts. Knowledge updates and abstention remain the weakest abilities across systems.

**MemBench** ([arXiv:2506.21605](https://arxiv.org/abs/2506.21605)): adds **effectiveness, efficiency (op count/latency), capacity (degradation as the store grows)** — the right template for memory dashboards. **MemoryAgentBench** ([arXiv:2507.05257](https://arxiv.org/abs/2507.05257)): four competencies — accurate retrieval, test-time learning, long-range understanding, **selective forgetting/updating** — and **no current system masters all four**; long-context baselines are consistently strong while dedicated memory frameworks show high variance and frequently underperform them. **PrefEval** ([arXiv:2502.09597](https://arxiv.org/abs/2502.09597), ICLR 2025 oral): **zero-shot preference-following falls below 10% just 10 turns after the preference was stated** — stated preferences evaporate without an explicit preference store.

**"Memory can hurt," quantified.** (1) **"The Power of Noise"** ([arXiv:2401.14887](https://arxiv.org/abs/2401.14887), SIGIR 2024): *related but non-answer-bearing* documents — precisely what nearest-neighbor memory returns — degrade accuracy **up to −67%**; random padding can even help. Semantic near-misses are the poison. (2) **GSM-IC** ([arXiv:2302.00093](https://arxiv.org/abs/2302.00093)): a single irrelevant sentence dramatically drops arithmetic accuracy. (3) LOCOMO's 2.1% adversarial + LongMemEval's 30–60% drop. (4) The add-all vs gated-write 3× gap. **Retrieval precision and abstention are first-order product features.** Economics justification when accuracy ties: Mem0 measured selective memory cutting **p95 latency 91% (17.12s → 1.44s) and tokens ~90%** vs full-context.

---

## 7. Failure-modes catalog

**Memory poisoning / injection-into-memory.** **AgentPoison** ([arXiv:2407.12784](https://arxiv.org/abs/2407.12784), NeurIPS 2024): **≥80% attack success at <0.1% poison rate**; triggers survive perplexity filters and rewriting. **MINJA** ([arXiv:2503.03704](https://arxiv.org/abs/2503.03704)): **>95% injection success through query-only interaction** — the agent itself writes attacker content into memory. **MemoryGraft** ([arXiv:2512.16962](https://arxiv.org/pdf/2512.16962)): one poisoned "experience" persists as a permanent backdoor. Microsoft's **failure-mode taxonomy** ([whitepaper](https://www.microsoft.com/en-us/security/blog/2025/04/24/new-whitepaper-outlines-the-taxonomy-of-failure-modes-in-ai-agents/), [v2.0 2026](https://www.microsoft.com/en-us/security/blog/2026/06/04/updating-taxonomy-failure-modes-agentic-ai-systems-year-red-teaming-taught-us/)): email assistant memorized instructions from a benign-looking email — **40% attack success, >80%** when prompts emphasized recall; in-the-wild "[AI recommendation poisoning](https://www.microsoft.com/en-us/security/blog/2026/02/10/ai-recommendation-poisoning/)" documented. **Mitigations that hold:** write-time gating + provenance + trust boundaries between memory and instructions + audit logs — *not* content filters.

**Stale preferences / failed knowledge updates.** PrefEval (<10% adherence after 10 turns): preferences must be *first-class updatable records*. **"Don't Ask the LLM to Track Freshness"** ([arXiv:2606.01435](https://arxiv.org/html/2606.01435v1)) measured conflict resolution: **Mem0 18%, Graphiti/Zep 7%, MemGPT/Letta 28%, HippoRAG-2 54%** — vs **78–94.8%** for a trivial pipeline where the LLM only *matches* candidate facts and deterministic code resolves the winner (`max(version)`). **Lesson: contradiction handling should be deterministic (timestamps/versions in SQL), with the LLM used only to detect that two memories concern the same fact.**

**Context bloat / retrieval distraction.** §6 numbers; plus "Lost in the Middle" ([arXiv:2307.03172](https://arxiv.org/abs/2307.03172)) *[numbers from training]* — position-dependent degradation.

**Consolidation staleness.** Summaries computed before an update bake in stale facts (LoCoMo's summary-RAG loss) — consolidation jobs must re-run or invalidate derived layers when underlying facts change.

**Privacy leakage across scopes.** **AirGapAgent** ([arXiv:2405.05175](https://arxiv.org/abs/2405.05175)): a single context-hijacking query drops prompt-level data protection **94% → 45%**; *architecturally minimizing which memory is even loaded* (contextual-integrity gating, escalation to the user) holds **97%** under the same attack. Memory rows need scope columns enforced in SQL, minimization at retrieval, HITL escalation for cross-scope reads.

**Error propagation / self-reinforcement.** Experience-following (§5): stored mistakes get imitated and re-stored; mitigations with numbers — strict write gating (3×) and history-based deletion (accuracy up, store −23%).

---

## DESIGN RULES THE LITERATURE SUPPORTS

1. **Type memory as CoALA's four stores** — working / episodic / semantic / procedural — with distinct write actions per store, because each has different risk and update semantics (CoALA, arXiv:2309.02427).
2. **Rank retrieval by recency × importance × relevance, not cosine alone**, with exponential decay on *last access* and write-time importance (Generative Agents, arXiv:2304.03442; ablation 29.89 vs 25.64).
3. **Gate every memory write** (and HITL for procedural/semantic promotions): strict admission tripled accuracy (13.05% → 38.50%, arXiv:2505.16067); write-gating is the mitigation class that survives poisoning (AgentPoison; Microsoft taxonomy).
4. **Record provenance on every row** and treat retrieved memory as data, never instructions — query-only attackers plant memories with >95% success (MINJA; Microsoft case study 40→80%).
5. **Resolve fact conflicts deterministically** — LLM detects same-fact-ness, versioned timestamps in code pick the winner — production systems score 7–28% vs **78–94.8%** for the deterministic recipe (arXiv:2606.01435).
6. **Store atomic facts alongside raw episodes; index at round level, not summary level**: fact-level beat summary retrieval 41.4 vs 29.9 F1 (LoCoMo); round-level indexing + fact keys ≈ +5% (LongMemEval).
7. **Timestamp everything; make retrieval time-aware** (temporal query expansion, time-filtered indexes): temporal reasoning is the weakest ability (20.3% vs human 92.6%); time-aware indexing recovers +7–11% (LongMemEval).
8. **Run consolidation offline in ambient mode** — reflection with an importance-sum trigger and evidence citations; hierarchical summaries with decay; RAPTOR trees — sleep-time compute cuts test-time compute ~5× and adds up to +13–18% accuracy (arXiv:2504.13171).
9. **Forget deliberately**: decay by access statistics, prune low-value records — deletion *improved* accuracy while shrinking the store 23–75% (arXiv:2505.16067; MemoryBank).
10. **Add graph structure only where association is the query pattern** (typed links/entity tags for multi-hop; community rollups for "across everything") — flat vector for single-hop, where graph adds ~nothing (HippoRAG; A-MEM; GraphRAG; Mem0g; arXiv:2412.15266).
11. **Retrieve few, high-precision items and support abstention** — near-miss distractors cost up to −67% (arXiv:2401.14887); one irrelevant sentence derails reasoning (GSM-IC); long-context models answer unanswerable questions 2.1% correctly (LoCoMo).
12. **Grow procedural memory as versioned, description-indexed skills with a promotion/demotion lifecycle**: additive skill libraries transfer without forgetting (Voyager); vote-counted insights self-correct and transfer (ExpeL; Reflexion for episode-level lessons).
13. **Enforce memory scopes in the storage layer and minimize what loads per task**, escalating cross-scope access: architectural minimization holds 97% where prompt-level collapses to 45% (AirGapAgent).
14. **Benchmark before trusting** — LongMemEval's five abilities + an adversarial set + MemBench's effectiveness/efficiency/capacity axes — dedicated memory frameworks routinely lose to plain long-context baselines (MemoryAgentBench), and when accuracy ties, the justification is cost/latency (91% p95 reduction, Mem0) — which must also be measured.

---

**Sources:** [CoALA](https://arxiv.org/abs/2309.02427) · [Generative Agents](https://arxiv.org/abs/2304.03442) · [1,000-people follow-up](https://arxiv.org/abs/2411.10109) · [MemoryBank](https://arxiv.org/abs/2305.10250) · [RAPTOR](https://arxiv.org/abs/2401.18059) · [MemGPT](https://arxiv.org/abs/2310.08560) · [Sleep-time Compute](https://arxiv.org/abs/2504.13171) / [Letta blog](https://www.letta.com/blog/sleep-time-compute/) · [HippoRAG](https://arxiv.org/abs/2405.14831) · [HippoRAG 2](https://arxiv.org/abs/2502.14802) · [A-MEM](https://arxiv.org/abs/2502.12110) · [GraphRAG](https://arxiv.org/abs/2404.16130) · [Reflexion](https://arxiv.org/abs/2303.11366) · [Voyager](https://arxiv.org/abs/2305.16291) · [ExpeL](https://arxiv.org/abs/2308.10144) · [LoCoMo](https://arxiv.org/abs/2402.17753) · [LongMemEval](https://arxiv.org/abs/2410.10813) · [MemBench](https://arxiv.org/abs/2506.21605) · [MemoryAgentBench](https://arxiv.org/abs/2507.05257) · [PrefEval](https://arxiv.org/abs/2502.09597) · [Power of Noise](https://arxiv.org/abs/2401.14887) · [GSM-IC](https://arxiv.org/abs/2302.00093) · [Lost in the Middle](https://arxiv.org/abs/2307.03172) · [AgentPoison](https://arxiv.org/abs/2407.12784) · [MINJA](https://arxiv.org/abs/2503.03704) · [MemoryGraft](https://arxiv.org/pdf/2512.16962) · [Microsoft taxonomy](https://www.microsoft.com/en-us/security/blog/2025/04/24/new-whitepaper-outlines-the-taxonomy-of-failure-modes-in-ai-agents/) / [v2.0](https://www.microsoft.com/en-us/security/blog/2026/06/04/updating-taxonomy-failure-modes-agentic-ai-systems-year-red-teaming-taught-us/) / [recommendation poisoning](https://www.microsoft.com/en-us/security/blog/2026/02/10/ai-recommendation-poisoning/) · [AirGapAgent](https://arxiv.org/abs/2405.05175) · [Freshness recipe](https://arxiv.org/html/2606.01435v1) · [Experience-following](https://arxiv.org/abs/2505.16067) · [Structural memory](https://arxiv.org/abs/2412.15266) · [Mem0 paper](https://arxiv.org/abs/2504.19413)
