# ADR-0006: JSONB embedding storage before pgvector

Status: Accepted

Date: 2026-08-07

## Context

Hybrid retrieval (ADR-0005) needs a vector per registry record. The standard
answer is pgvector — but pgvector requires a non-stock Postgres image (or an
extension install step), an index strategy, and query-side ANN plumbing. At
POC scale the entire catalog already sits in the registry cache snapshot and
is scored in-process; the database is never asked a similarity question.
Adding pgvector would have complicated the three-service compose stack
(ADR-0001) to accelerate a query nobody runs.

## Decision

- Embeddings are stored as **JSONB float arrays** on the registry rows
  themselves: `embedding` + `embedding_hash` columns on tools, skills, and
  sub_agents (the models map `list[Any] → JSONB` via the declarative
  `type_annotation_map` in `backend/app/models/base.py`). The stock
  `postgres:16` image keeps working; no extension, no new service.
- Cosine similarity is computed in-process over the cache snapshot, next to
  BM25 (ADR-0005). The `embedding_hash` (SHA-256 of the embedded text)
  makes re-embedding idempotent: unchanged text is never re-sent.
- Embedding **generation lives on the provider port** (ADR-0002):
  `supports_embeddings()` / `get_embeddings(model, texts)` with the single
  entry point `get_embeddings("provider:model", texts)`. OpenAI and Google
  adapters implement it; **Anthropic reports unsupported and raises** —
  consumers degrade to lexical-only scoring, silently. Embeddings are
  maintained best-effort on the write path (failure logs and leaves the row
  unembedded, never fails the save) plus a startup backfill.
- **pgvector is the documented storage swap**, not a rejected option: when
  catalogs outgrow in-memory ranking, the JSONB column migrates to a
  `vector` column and scoring moves database-side. Spec §7.3 records this
  explicitly so the future migration is a planned step, not a rewrite.

## Consequences

Positive:

- Zero new infrastructure; the compose stack and CI images are untouched.
- Retrieval works with any embedding provider or none — an Anthropic-only
  deployment still gets BM25 ranking with no configuration.
- The swap path is clean: storage format changes, but `embed_text_for()`,
  hashing, and the write-path hooks all survive a pgvector migration.

Negative:

- In-process cosine is O(records × dimensions) per query; past a few
  thousand records this visibly costs latency and the migration becomes
  due — the design deliberately defers, it does not solve.
- JSONB float arrays are storage-inefficient versus a packed vector type,
  and Postgres cannot index them for similarity at all.
- Best-effort write-path embedding means rows can silently lack vectors
  (provider outage, missing key); ranking quality then varies per row in a
  way that is only visible in logs.

## References

- spec.md §2.1 (embeddings on the port), §7.3 ("pgvector remains a
  documented storage swap"), §7.4 (lexical-only degradation)
- /home/user/concierge-agent/backend/app/models/base.py, tool.py, skill.py,
  sub_agent.py (embedding columns)
- /home/user/concierge-agent/backend/app/retrieval.py (`embed_text_for`,
  `text_hash`, cosine + RRF)
- /home/user/concierge-agent/backend/app/llm/adapters.py (per-provider
  `get_embeddings`, Anthropic unsupported)
- Related: ADR-0002 (provider port), ADR-0005 (hybrid retrieval)
