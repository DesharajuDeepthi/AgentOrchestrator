# Memory layers — research suite

Deep research for the `memory_xperiment` branch: what memory layers this
platform should grow, grounded in what it already has, what the field has
shipped, what the literature has measured, and what the substrate supports.
Research date: 2026-08-20 (three parallel research agents with live web
verification + codebase grounding).

## Reading order

| Doc | What it holds | Read it for |
|---|---|---|
| [01-current-state.md](01-current-state.md) | Verified inventory of memory-adjacent machinery already in this codebase, mapped to the CoALA taxonomy | why this is a promotion of existing assets, not a bolt-on |
| [02-landscape-production.md](02-landscape-production.md) | Letta/MemGPT, Mem0, Zep/Graphiti, LangGraph store + LangMem, Anthropic memory tool + CLAUDE.md school, ChatGPT/Vertex/Bedrock memory — mechanisms, APIs, weaknesses, top-12 transferable mechanisms | which shipped ideas transfer |
| [03-landscape-research.md](03-landscape-research.md) | CoALA, Generative Agents, MemoryBank, RAPTOR, sleep-time compute, HippoRAG/A-MEM/GraphRAG, Reflexion/Voyager/ExpeL, LOCOMO/LongMemEval/MemBench, poisoning + failure modes — 14 evidence-backed design rules | the numbers behind every design call |
| [04-substrate-postgres.md](04-substrate-postgres.md) | pgvector 0.8.6 operational guidance, hybrid RRF SQL, AsyncPostgresStore/LangMem verdicts (source-verified), bi-temporal SQL patterns, advisory-lock consolidation, embedding side-table strategy — 6 substrate decisions | how it fits in one Postgres, three services |
| [05-architecture-proposal.md](05-architecture-proposal.md) | **The design**: L0–L4 layer map, schemas, gate→extract→reconcile write path, injection plane, procedural learning, consolidation scheduler, governance/UI, failure-mode table, settled decisions | what we propose to build |
| [06-spec-amendment-and-milestones.md](06-spec-amendment-and-milestones.md) | Draft spec §16 text, touch-ups to §3.7/§7.5/§8/§12/§14, milestones **M13–M17**, settled decisions + open questions for sign-off | what merges into spec.md, and in what order |
| [07-experiment-results.md](07-experiment-results.md) | **The measurements**: live layer-ablation matrix (off/episodic/semantic/full/tight) — accuracy, tokens, latency per layer; fallback-rate experiment; defects the experiment itself surfaced; recommended defaults | which layers earn their keep, with numbers |

## The design in one paragraph

Memory is five layers over stores the platform largely already has: **L0
working** (the existing history window + SummarizationMiddleware, now with
named per-source token budgets), **L1 episodic** (the runs ledger made
retrievable through per-run digests and conversation rollups), **L2 semantic**
(a new bi-temporal `memories` table — facts, preferences, entities, relations,
instructions — written through a strict admission gate and an
LLM-matches/code-resolves reconciliation, with instruction-kind rows always
HITL-quarantined), **L3 procedural** (the registry, plus routing stats, plan
exemplars with an ExpeL vote lifecycle, and fallback-mining that drafts
`.skill.md` proposals through doclint + the overlap judge into a human review
queue), and **L4 consolidation** (idle-time, advisory-locked asyncio jobs:
reflection with evidence citations, Ebbinghaus decay, contradiction sweeps —
the ambient-mode runway). Agent-facing memory operations are **registry
citizens** (`memory.recall/remember/forget` as native tools behind the same
exposure gates as everything else), the store is fully user-visible and
editable in a new Memory UI page, everything is dark by default
(`memory_enabled=false` is byte-identical to today), and the eval suite
measures LongMemEval's five abilities plus token/latency — with M16's success
criterion being a measured drop in the stage-30 fallback rate.

## Status

Research + design complete; nothing implemented. Spec amendment (06) awaits
review — including one explicit sign-off: the `pgvector/pgvector:0.8.6-pg16`
image pin. Implementation starts at M13 after the amendment merges into
`spec.md` (per CLAUDE.md: spec first, then code).
