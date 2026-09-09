# ADR-0005: Hybrid BM25 + cosine retrieval with RRF, dark by default

Status: Accepted

Date: 2026-08-06

## Context

Progressive disclosure keeps orchestrator prompts small by injecting compact
catalog summaries. That works while registries hold tens of records; once an
MCP-heavy deployment ingests hundreds of tools, full injection blows up token
cost and degrades routing quality. The POC needed the production answer in
place — ranked, truncated catalogs — without changing the behavior the
acceptance evidence was captured against.

## Decision

Top-K progressive-disclosure retrieval (spec §7.4), applied **only to the
catalogs the orchestrator sees** — the graph-mode planner catalog and the
agentic mode's exposed-mode middleware projections:

- **Off by default** (`retrieval_enabled=false`), and even when enabled it
  activates per registry only above `retrieval_threshold` records; below
  that, full injection exactly as before, bit-for-bit.
- **Scoring** runs in-process over the registry-cache snapshot (never a
  per-call DB query): Okapi BM25 over name + description (no external
  dependency — catalogs are hundreds of records, not millions) fused with
  vector cosine over stored embeddings via **reciprocal-rank fusion** of
  whichever score lists exist. The query text is the current task — plan
  entry text in graph mode, latest user message/todo in agentic mode — with
  query embeddings memoized per task text.
- **Never ranked**: ids referenced by the current plan and entities already
  used in the run are pinned past ranking; skill loops, sub-agent workflows,
  and `spin_worker` are id-pinned contracts and are never subject to
  retrieval.
- **Escape hatch**: full-catalog mode (the §7.0 fallback and the explicit
  `use_full_catalog` escalation tool) bypasses retrieval entirely, so a
  top-K miss is always recoverable. Every truncation logs its drop count and
  the injected catalog carries a footer ("showing N of M — use_full_catalog
  to widen") so the model knows it sees a slice.

## Consequences

Positive:

- Token cost of disclosure becomes O(K) instead of O(catalog), with a
  smooth activation ramp (threshold) instead of a cliff.
- Hybrid scoring degrades gracefully: no embedding model configured means
  lexical-only, silently (see ADR-0006) — retrieval never becomes a hard
  dependency on any provider.
- A retrieval miss is a recoverable routing event, not a dead end: pinning,
  the footer, and full-catalog fallback each cover a failure class.

Negative:

- Dark-by-default means the ranking path gets little production exercise
  until someone flips it on; quality regressions can hide.
- BM25 over short name/description strings is crude — records with sparse
  prose rank poorly, which re-amplifies the system's existing dependency on
  good registry descriptions.
- RRF's rank-only fusion discards score magnitudes; two mediocre signals
  can outvote one excellent one.
- More knobs (`retrieval_enabled`, threshold, top-K) for operators to
  misconfigure.

## References

- spec.md §7.4 (progressive-disclosure retrieval), §7.0 (full-catalog
  fallback)
- /home/user/concierge-agent/backend/app/retrieval.py
- /home/user/concierge-agent/docs/acceptance/18-registry-cache-and-retrieval/
- Related: ADR-0004 (cache snapshot as the scoring source), ADR-0006
  (embedding storage and degradation)
