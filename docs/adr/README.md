# Architecture Decision Records

Decisions that shaped the Concierge Agent POC. The spec (`spec.md`) is the
source of truth for behavior; these records capture *why* the load-bearing
choices were made and what they cost. Format follows the classic
Context / Decision / Consequences structure.

| # | Title | Status | Date |
|---|---|---|---|
| [0001](./0001-no-broker-single-process.md) | Single asyncio FastAPI process, no message broker | Accepted | 2026-08-04 |
| [0002](./0002-model-provider-port.md) | ModelProvider port and adapter registry for all model access | Accepted | 2026-08-04 |
| [0003](./0003-middleware-precedence.md) | Middleware precedence — out-of-box first, custom last | Accepted | 2026-08-05 |
| [0004](./0004-registry-cache-bypass-default.md) | RegistryCache facade with bypass as the shipped default | Accepted | 2026-08-06 |
| [0005](./0005-hybrid-retrieval-bm25-rrf.md) | Hybrid BM25 + cosine retrieval with RRF, dark by default | Accepted | 2026-08-06 |
| [0006](./0006-jsonb-embeddings-before-pgvector.md) | JSONB embedding storage before pgvector | Accepted | 2026-08-07 |
| [0007](./0007-openai-responses-api-routing.md) | Route OpenAI reasoning-effort runs through the Responses API | Accepted | 2026-08-07 |
| [0008](./0008-listen-notify-cross-replica.md) | Postgres LISTEN/NOTIFY for cross-replica cache invalidation | Accepted | 2026-08-07 |
| [0009](./0009-skills-as-markdown.md) | Skills as markdown documents — one format, two homes | Accepted | 2026-08-04 |
| [0010](./0010-two-orchestrator-modes.md) | Two orchestrator modes, side by side and runtime-switchable | Accepted | 2026-08-05 |
