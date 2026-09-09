# Memory research 04 — storage/retrieval substrate (Postgres-first)

> Researched 2026-08-20 against live sources by a dedicated research agent
> (versions verified on PyPI/Docker Hub/GitHub that day; the few from-training
> claims are marked). Companion docs: 01 (current state), 02 (production),
> 03 (academic), 05 (proposal), 06 (spec amendment).

Scope: Postgres 16 in docker compose (three services, no new infra), FastAPI +
async SQLAlchemy + Alembic, LangChain/LangGraph with the OOB-first rule, single
asyncio process, provider-agnostic `get_model()` embeddings, existing
LISTEN/NOTIFY invalidation bus.

---

## 1. pgvector NOW

**Current version: 0.8.6 (released 2026-07-29).** The 0.8.x line has been in hardening mode: 0.8.2 fixed a buffer overflow in parallel HNSW builds, 0.8.3/0.8.4 fixed HNSW-vacuum index-corruption and insert-during-vacuum issues, 0.8.5/0.8.6 reduced IVFFlat build memory ([CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md)). Practical takeaway: pin ≥ 0.8.4 to get the HNSW vacuum fixes — relevant for a memory table with churn.

**Types** ([README](https://github.com/pgvector/pgvector)): `vector` (max 16,000 dims, **indexable up to 2,000**), `halfvec` (fp16, indexable up to **4,000** dims, ~half the storage), `sparsevec` (only worth it for learned sparse embeddings like SPLADE), `bit` (binary quantization).

**Indexes:**
- **HNSW** — build params `m=16`, `ef_construction=64` (defaults); query-time `SET hnsw.ef_search = 40` (default). Better speed/recall than IVFFlat, slower builds, more memory; can be built on an **empty table** (it's incremental), which is exactly what a grow-from-zero memories table needs.
- **IVFFlat** — `lists ≈ rows/1000` up to 1M rows, `probes ≈ sqrt(lists)`. Needs pre-existing data for k-means training; recall degrades as data drifts. Wrong choice for a table that starts empty.
- Build speed: raise `maintenance_work_mem` and `max_parallel_maintenance_workers`; in Docker set compose `shm_size` ≥ `maintenance_work_mem` for parallel builds.

**Filtering + the over-filtering problem.** With ANN indexes, `WHERE` clauses apply **after** the index scan: an HNSW scan retrieves ~`ef_search` candidates, then the filter discards non-matching ones, so a `LIMIT 10` query can silently return 3 rows (or zero). Fixes, per the README: (a) **iterative index scans** (since 0.8.0, **off by default**): `SET hnsw.iterative_scan = strict_order | relaxed_order` — the scan resumes deeper into the index until enough rows survive, bounded by `hnsw.max_scan_tuples` (default 20,000) and `hnsw.scan_mem_multiplier`; (b) b-tree indexes on filter columns; (c) **partial indexes** per hot filter value; (d) partitioning for multi-tenant.

**Operational guidance for <1M rows:** squarely pgvector's comfort zone. One HNSW index with defaults, `ef_search` raised to 60–100, `hnsw.iterative_scan = relaxed_order` per-query (`SET LOCAL`) on filtered searches. At <100k memories × 1536 dims the index is tens of MB — RAM-resident, single-digit-ms queries. No quantization needed.

**Docker image:** the standard `pgvector/pgvector` image actively tracks Postgres 16 — tags `pg16`, `pg16-bookworm`, `pg16-trixie`, pinned `0.8.6-pg16`, all pushed **2026-08-13** ([Docker Hub tags](https://hub.docker.com/v2/repositories/pgvector/pgvector/tags?page_size=30&name=pg16)). It is the official `postgres` image + the extension, so contrib modules (`btree_gist`, `pg_trgm`) are present.

**pgvectorscale (StreamingDiskANN):** Timescale's extension on top of pgvector; latest **0.9.0** ([releases](https://github.com/timescale/pgvectorscale/releases/tag/0.9.0)). Its value starts when the HNSW graph no longer fits in RAM — roughly 10M+ vectors ([comparison](https://www.web3aiblog.com/blog/postgres-vector-search-compared-pgvector-pgvectorscale-paradedb-lantern-2026), [Tiger Data](https://www.tigerdata.com/blog/pgvector-is-now-as-fast-as-pinecone-at-75-less-cost)). For thousands-to-hundreds-of-thousands of memory rows it is pure overkill (plain HNSW is *faster* in the RAM-resident regime).

---

## 2. Hybrid retrieval in one Postgres

**Lexical options.** Stock Postgres 16 gives `tsvector` + GIN + `ts_rank`/`ts_rank_cd`. Known weaknesses: no IDF term (one labeled benchmark shows nDCG@10 of 0.07 vs 0.69 for true BM25, per [ParadeDB](https://www.paradedb.com/learn/search-in-postgresql/bm25)), and `ts_rank` cannot be computed from the GIN index alone, so ranked top-k reads every matching heap tuple ([VectorChord analysis](https://blog.vectorchord.ai/postgresql-full-text-search-fast-when-done-right-debunking-the-slow-myth)). True BM25 requires an extra extension (**ParadeDB `pg_search`**, VectorChord-bm25, pg_textsearch) — none ship in the `pgvector/pgvector` image. **At memory-subsystem scale (thousands of short texts), tsvector + `ts_rank_cd` is entirely adequate**, and RRF blunts the ranking-quality gap because fusion consumes *rank positions*, not raw scores.

**RRF over two CTEs** — the canonical shape (adapted from [Supabase's hybrid-search recipe](https://supabase.com/docs/guides/ai/hybrid-search); `rrf_k` anywhere 40–60 is fine):

```sql
WITH lexical AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(fts, q) DESC) AS rank_ix
    FROM memories, websearch_to_tsquery('english', :query_text) q
    WHERE fts @@ q
      AND superseded_at IS NULL AND status = 'active'
    ORDER BY rank_ix
    LIMIT LEAST(:match_count, 30) * 2
),
semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> :query_emb) AS rank_ix
    FROM memories
    WHERE superseded_at IS NULL AND status = 'active'
    ORDER BY embedding <=> :query_emb          -- bare operator, ASC: index-servable
    LIMIT LEAST(:match_count, 30) * 2
)
SELECT m.*,
       COALESCE(1.0 / (:rrf_k + lexical.rank_ix), 0.0)  * :full_text_weight
     + COALESCE(1.0 / (:rrf_k + semantic.rank_ix), 0.0) * :semantic_weight AS rrf_score
FROM lexical
FULL OUTER JOIN semantic ON lexical.id = semantic.id
JOIN memories m ON m.id = COALESCE(lexical.id, semantic.id)
ORDER BY rrf_score DESC
LIMIT :match_count;
```

Notes: `fts` is a stored generated column (`GENERATED ALWAYS AS (to_tsvector('english', text)) STORED`) with a GIN index; the semantic CTE's shape (`ORDER BY embedding <=> $1 ... LIMIT n`) is exactly what HNSW can serve; run with `SET LOCAL hnsw.iterative_scan = relaxed_order` because of the status/supersession filters.

**Recency/importance re-scoring (Generative-Agents-style).** Park et al.'s score is `α_rec·recency + α_imp·importance + α_rel·relevance` with exponential recency decay (0.995/hour ≈ 5.8-day half-life). Compose it *on top of* the RRF CTE over the fused candidates:

```sql
SELECT id, text,
       :w_rel * (1 - (embedding <=> :query_emb))
     + :w_imp * (importance / 10.0)
     + :w_rec * exp(-ln(2) / :half_life_hours *
                    GREATEST(EXTRACT(EPOCH FROM (now() - last_accessed_at))/3600.0, 0))
       AS score
FROM candidates            -- the top-K (e.g. 50) from the ANN/RRF stage
ORDER BY score DESC
LIMIT :k;
```

Do the weighted scoring over a **candidate set from the index** (top 50–100), never the whole table — the decay expression is not indexable. `last_accessed_at` updates on retrieval (batched, never per-read-per-row in the hot path) to implement "rehearsal strengthens memory."

---

## 3. LangGraph AsyncPostgresStore and LangMem — deep-dive

**Package:** `langgraph-checkpoint-postgres`, current **3.1.2 (2026-08-07)**, psycopg3-based ([PyPI](https://pypi.org/project/langgraph-checkpoint-postgres/)); contains **both** `AsyncPostgresSaver` (checkpointer) and `langgraph.store.postgres.AsyncPostgresStore`. Companions: `langgraph` 1.2.11, `langgraph-checkpoint` 4.2.0.

**Division of labor** ([docs](https://docs.langchain.com/oss/python/langgraph/add-memory)): the **checkpointer** is thread-scoped short-term memory (already in use here for HITL resume). The **store** is cross-thread long-term memory, injected into nodes via `Runtime`/`get_store()`.

**Store schema** (read directly from [base.py on main](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/store/postgres/base.py)):
- `store(prefix TEXT, key TEXT, value JSONB, created_at, updated_at, expires_at, ttl_minutes, PK (prefix, key))` with a `text_pattern_ops` btree on prefix and a partial index on `expires_at`.
- `store_vectors(prefix, key, field_name, embedding, PK (prefix,key,field_name), FK → store ON DELETE CASCADE)`.
- Migration bookkeeping in `store_migrations` / `vector_migrations`; the vector migration runs `CREATE EXTENSION vector` itself.

**Semantic-search config** (`PostgresIndexConfig`): `dims`, `embed` (a LangChain `Embeddings` instance — the `get_model()` layer output plugs in), `fields`, `distance_type` (default cosine), `ann_index_config` (`hnsw` default | `ivfflat` | `flat`; `vector` | `halfvec`). Search supports `$eq/$ne/$gt/$gte/$lt/$lte` filters on JSONB fields.

**The catch, verified in source:** the store creates an HNSW index by default, but the query builder carries this comment: *"Note: Today, we are not using ANN indices due to restrictions on PGVector's support for mixing vector and non-vector filters"* — the search SQL wraps the distance in a CTE with namespace-prefix WHERE clauses in a shape the planner won't serve from HNSW, so **semantic search in AsyncPostgresStore effectively sequential-scans `store_vectors`** (referencing [pgvector#216](https://github.com/pgvector/pgvector/issues/216)). Fine at ≤ tens of thousands of rows; a real ceiling beyond that, unfixable without forking their SQL.

**TTL:** `TTLConfig{refresh_on_read: bool = True, default_ttl: float|None (minutes), sweep_interval_minutes}`; requires `store.start_ttl_sweeper()` (asyncio task, default 5-minute interval — verified in `aio.py`). Fits the single-process constraint.

**`.setup()` vs Alembic:** `setup()` runs its own versioned migrations — a second, non-Alembic migration authority in the same schema. Coexistence pattern: call `setup()` once in lifespan under a pg advisory lock, and teach Alembic autogenerate to ignore the LangGraph tables (`include_object` name filtering). A known friction, not a blocker.

**LangMem on top.** Package `langmem`, latest **0.0.30, uploaded 2025-10-27** — ~10 months without a release, pre-1.0; not archived; LangChain docs still position it as the long-term-memory layer ([PyPI](https://pypi.org/project/langmem/), [repo](https://github.com/langchain-ai/langmem)). APIs: `create_manage_memory_tool` / `create_search_memory_tool` (hot-path tools over any `BaseStore`); `create_memory_store_manager(...)` (searches similar, extracts/consolidates via trustcall patching, upserts, versions; `query_limit=5`, namespace templates); `ReflectionExecutor.submit(..., after_seconds=N)` for debounced background reflection. **Honest assessment:** conceptually exactly the consolidation loop needed, but: 0.0.x; stale cadence against fast-moving langgraph 1.x; and it **hard-depends on both `langchain-anthropic` and `langchain-openai`** (verified in PyPI metadata) — it installs provider SDKs into an environment whose ground rule is "no provider packages outside `app/llm/`". Verdict: **steal its patterns (extract → search-similar → consolidate/supersede → upsert; debounced reflection), don't take the dependency.**

---

## 4. Bi-temporal + supersession patterns (no extensions beyond contrib)

Two time axes — *valid time* (true in the world) and *transaction time* (known to the system). Plain columns:

```sql
CREATE TABLE memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    entity_key      TEXT,                        -- optional: what fact this is "about"
    importance      SMALLINT NOT NULL DEFAULT 5, -- 1..10
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,                 -- NULL = still true
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at   TIMESTAMPTZ,                 -- NULL = current belief
    superseded_by   UUID REFERENCES memories(id),
    supersedes      UUID REFERENCES memories(id),
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);

CREATE INDEX memories_current_ns_idx ON memories (namespace, kind)
    WHERE superseded_at IS NULL;                 -- hot path touches only current rows
CREATE INDEX memories_fts_idx ON memories USING gin (fts);

CREATE VIEW memories_current AS
    SELECT * FROM memories
    WHERE superseded_at IS NULL
      AND valid_from <= now() AND (valid_to IS NULL OR valid_to > now());
```

**Supersession is append-only:** never `UPDATE text`; insert the replacement and close the old row in one transaction:

```sql
WITH new_mem AS (
    INSERT INTO memories (namespace, kind, text, supersedes, valid_from)
    VALUES (:ns, :kind, :new_text, :old_id, :valid_from) RETURNING id
)
UPDATE memories SET superseded_at = now(), superseded_by = (SELECT id FROM new_mem)
WHERE id = :old_id AND superseded_at IS NULL;   -- guard: no double-supersede
```

Chains walk with a recursive CTE. **Point-in-time queries** — "what did the agent believe at T about facts valid at V":

```sql
SELECT * FROM memories
WHERE recorded_at <= :T AND (superseded_at IS NULL OR superseded_at > :T)   -- known then
  AND valid_from  <= :V AND (valid_to      IS NULL OR valid_to      > :V);  -- true then
```

**Exclusion constraint** (optional, when `entity_key` marks single-valued facts): `btree_gist` is stock contrib —

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE memories ADD CONSTRAINT no_overlapping_current_validity
    EXCLUDE USING gist (
        namespace WITH =, entity_key WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    ) WHERE (superseded_at IS NULL AND entity_key IS NOT NULL);
```

Use it only if concurrent writers could race on the same fact; otherwise the `WHERE superseded_at IS NULL` optimistic guard suffices.

---

## 5. Single-process background consolidation

**Lifespan-owned periodic task:**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()
    tasks = [asyncio.create_task(consolidation_loop(stop)),
             asyncio.create_task(notify_listener(stop))]
    yield
    stop.set()
    for t in tasks: t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
```

**Advisory lock so only one replica consolidates** ([docs](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)): two-key form with a fixed classid and a job id — `SELECT pg_try_advisory_lock(42001, <job_id>)`, non-blocking, skip the cycle if false. Session-level locks are tied to the **connection**: hold one dedicated connection for the pass, unlock (or close) in `finally`. For single-transaction work, `pg_try_advisory_xact_lock` auto-releases and cannot leak. Derive keys via `hashtext('memory:consolidation')::int` if hand-picked integers offend.

**LISTEN/NOTIFY invalidations** — the bus exists; memory-specific guidance: `pg_notify('memory_invalidated', json_build_object('ns', :ns, 'ids', :ids)::text)` after commit. Constraints ([NOTIFY docs](https://www.postgresql.org/docs/16/sql-notify.html)): payload ≤ **8000 bytes** — send namespace + ids, never memory bodies; notifications deliver **on commit**; duplicates within a transaction are de-duplicated. Listener: dedicated **autocommit** connection, trivial handler (set dirty flag / evict key). LISTEN/NOTIFY is fire-and-forget — a reconnecting replica missed everything, so treat it as a cache hint with a periodic full-refresh fallback; the table is the truth.

**Pitfalls:**
- **Long transactions vs vacuum:** an LLM call inside an open transaction pins the xmin horizon and blocks vacuum for the whole database. Pattern: read candidates and **commit**, call the LLM with no transaction open, apply writes in a short transaction with optimistic guards (`WHERE superseded_at IS NULL`).
- **Index bloat from churn:** supersession = inserts + updates; HNSW tombstones are reclaimed only by vacuum (and pre-0.8.3/0.8.4 had vacuum bugs). Aggressive autovacuum on `memories` (`autovacuum_vacuum_scale_factor ≈ 0.02`), occasional `REINDEX INDEX CONCURRENTLY` if recall drifts.
- CPU-heavy scoring goes to `asyncio.to_thread`; LLM calls are awaits and fine.

---

## 6. Embedding operations under provider-agnosticism

**The tension:** pgvector ANN indexes require a **fixed dimension per column/index**, but `embedding_model` is a runtime setting. Three strategies:

1. **Fixed typed column** (`vector(1536)`): simplest; a model switch with different dims is a migration + full re-embed.
2. **Versioned columns**: explicit but schema churn.
3. **Embeddings side-table** — the pattern the pgvector README itself documents for mixed dimensions ([README FAQ](https://github.com/pgvector/pgvector#can-i-store-vectors-with-different-dimensions-in-the-same-column)):

```sql
CREATE TABLE memory_embeddings (
    memory_id  UUID REFERENCES memories(id) ON DELETE CASCADE,
    model_key  TEXT NOT NULL,          -- 'openai:text-embedding-3-small@1536'
    embedding  vector NOT NULL,        -- untyped: any dims
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, model_key)
);
-- one partial expression index per ACTIVE model (created by the re-embed pipeline):
CREATE INDEX CONCURRENTLY memory_emb_oai3s_idx ON memory_embeddings
    USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
    WHERE model_key = 'openai:text-embedding-3-small@1536';
```

Queries pin `WHERE model_key = :active` and `ORDER BY embedding::vector(1536) <=> :q` (cast must match the index expression). **Model switch = background re-embed pipeline**: advisory-locked asyncio task, batches of 100–500 via `get_embeddings`, `CREATE INDEX CONCURRENTLY`, config flip; old and new coexist — zero-downtime switch.

**Cost/latency of re-embedding:** at current prices (OpenAI `text-embedding-3-small` **$0.02/1M tokens** ([pricing](https://www.cloudzero.com/blog/openai-pricing/)); `voyage-3.5-lite` likewise ([announcement](https://www.mongodb.com/company/blog/product-release-announcements/introducing-voyage-3-5-voyage-3-5-lite-improved-quality-new-retrieval-frontier))) — 100k memories × ~60 tokens ≈ 6M tokens ≈ **$0.12**. Latency, not cost, is the constraint (~10–30 min background at rate limits). **Do not over-engineer to avoid re-embedding**; the side-table just makes it non-disruptive.

---

## 7. What stock Postgres 16 + pgvector CANNOT do well

| Gap | Why | POC-scale mitigation |
|---|---|---|
| **True BM25 ranking** | `ts_rank` lacks IDF; ranked top-k reads matching heap tuples | RRF fusion (rank-based); corpus is small; `pg_search` only if lexical quality measurably fails |
| **Graph traversal at depth** | no graph engine; recursive joins blow up at depth ≥ 3 | recursive CTEs capped at 1–2 hops over a `memory_edges` table + visited guard; supersession chains are shallow |
| **Sub-ms ANN at 10M–1B vectors** | HNSW must be RAM-resident | irrelevant <1M rows; escape hatch = pgvectorscale or partitioning |
| **Index-served "filter + ANN" in one plan** | filters applied post-scan (§1) | `iterative_scan = relaxed_order` + partial indexes per hot predicate |
| **Query-time dimension flexibility** | fixed dims per index | side-table + partial expression indexes (§6) |
| **Reliable cross-replica eventing** | NOTIFY is at-most-once for disconnected listeners, 8KB | cache hint only; truth in tables; periodic reconcile sweep |
| **Learned sparse retrieval (SPLADE)** | needs a sparse-embedding pipeline | skip; tsvector covers the lexical leg |

---

## RECOMMENDED SUBSTRATE DECISIONS

1. **Image/extension:** `pgvector/pgvector:0.8.6-pg16` (digest-pin; verified current 2026-08-13). Extensions: `vector` + contrib `btree_gist`. **No pgvectorscale, no pg_search/ParadeDB** — both force a custom image for capability POC scale doesn't need.
2. **Index types + params:** HNSW everywhere (works on empty tables); defaults `m=16, ef_construction=64`; query with `SET LOCAL hnsw.ef_search = 80` and `SET LOCAL hnsw.iterative_scan = relaxed_order` on all filtered vector queries. GIN on the generated `fts` column. Partial b-tree `(namespace, kind) WHERE superseded_at IS NULL`. No IVFFlat.
3. **Table strategy for embeddings:** own `memories` table (Alembic-managed, bi-temporal per §4) + `memory_embeddings(memory_id, model_key, embedding vector /*untyped*/)` side-table with **one partial expression HNSW index per active model_key** (§6). Model switch = advisory-locked background re-embed + `CREATE INDEX CONCURRENTLY` + config flip. Store `model_key` (provider:model@dims) on every row.
4. **LangGraph store vs native tables:** keep **`AsyncPostgresSaver` for thread-scoped checkpoints — exactly OOB-first** — but put **long-term memory in native Alembic-managed tables, not `AsyncPostgresStore`**. Reasoning: (a) verified in source, its semantic search doesn't use the HNSW index it builds (sequential scan by design); (b) its flat `(prefix,key,value)` schema cannot express supersession, bi-temporality, importance, or composite scoring — the memory subsystem's core semantics — so "custom" is justified under the OOB-first rule as *nothing OOB fits*; (c) it brings a second migration authority conflicting with "one Alembic migration per schema change." Do **not** adopt `langmem` as a dependency (0.0.30, 10 months stale, hard-depends on `langchain-anthropic`+`langchain-openai`, violating provider isolation); reimplement its extract→search-similar→consolidate→supersede loop (~200 lines) with prompts in `backend/app/prompts/`.
5. **Retrieval recipe:** two-CTE RRF exactly as §2 (candidate limit `2×k` per leg, `rrf_k = 50`, `FULL OUTER JOIN`, `websearch_to_tsquery`), followed by the weighted re-score `w_rel·cos_sim + w_imp·importance/10 + w_rec·exp(−ln2·age_h/half_life)` over the fused top-50, defaults `w = (1.0, 0.6, 0.8)`, half-life 168h — one SQL statement, parameters in config.
6. **Lock/notify pattern:** consolidation loop as a lifespan asyncio task; each cycle `pg_try_advisory_lock(42001, <job_id>)` on a dedicated connection (skip if false; unlock in `finally`); never hold a DB transaction across an LLM call — read/commit, call, write/commit with optimistic guards. After commit, `pg_notify('memory_invalidated', ...)` (≤8KB) onto the existing bus; listeners only evict cache entries — Postgres rows remain the sole truth, with a periodic reconcile sweep covering missed notifications.

**Sources:** [pgvector CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md) · [pgvector README](https://github.com/pgvector/pgvector) · [pgvector Docker Hub tags](https://hub.docker.com/v2/repositories/pgvector/pgvector/tags?page_size=30&name=pg16) · [pgvectorscale 0.9.0](https://github.com/timescale/pgvectorscale/releases/tag/0.9.0) · [Postgres vector search compared 2026](https://www.web3aiblog.com/blog/postgres-vector-search-compared-pgvector-pgvectorscale-paradedb-lantern-2026) · [Tiger Data DiskANN](https://www.tigerdata.com/blog/pgvector-is-now-as-fast-as-pinecone-at-75-less-cost) · [Supabase hybrid search / RRF](https://supabase.com/docs/guides/ai/hybrid-search) · [ParadeDB BM25 explainer](https://www.paradedb.com/learn/search-in-postgresql/bm25) · [pg_search on PGXN](https://pgxn.org/dist/pg_search/) · [VectorChord FTS analysis](https://blog.vectorchord.ai/postgresql-full-text-search-fast-when-done-right-debunking-the-slow-myth) · [LangGraph add-memory docs](https://docs.langchain.com/oss/python/langgraph/add-memory) · [AsyncPostgresStore reference](https://reference.langchain.com/python/langgraph.store.postgres/aio/AsyncPostgresStore) · [store source (schema/ANN comment)](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/store/postgres/base.py) · [langgraph-checkpoint-postgres PyPI](https://pypi.org/project/langgraph-checkpoint-postgres/) · [LangMem docs](https://langchain-ai.github.io/langmem/) · [langmem PyPI](https://pypi.org/project/langmem/) · [langmem repo](https://github.com/langchain-ai/langmem) · [OpenAI pricing 2026](https://www.cloudzero.com/blog/openai-pricing/) · [Voyage 3.5 announcement](https://www.mongodb.com/company/blog/product-release-announcements/introducing-voyage-3-5-voyage-3-5-lite-improved-quality-new-retrieval-frontier) · Postgres 16 docs for [NOTIFY](https://www.postgresql.org/docs/16/sql-notify.html), [advisory locks](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS), [btree_gist](https://www.postgresql.org/docs/16/btree-gist.html).
