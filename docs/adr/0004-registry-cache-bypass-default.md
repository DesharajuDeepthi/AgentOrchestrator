# ADR-0004: RegistryCache facade with bypass as the shipped default

Status: Accepted

Date: 2026-08-06

## Context

After M6, every run-path read (the three registry middlewares, the graph-mode
planner catalog, the resolution ladder, the worker factory's id lookups, the
settings store) executed direct Postgres queries per model call. Correct, but
not production-shaped: a busy orchestrator re-reads small, rarely-changing
catalogs on every loop iteration. M7 had to add a cache without violating the
system's freshness contract ("visible at the next model call") and without
risking a regression in behavior the acceptance campaign had already proven.

## Decision

All run-path registry and settings reads go through one `RegistryCache`
facade (`backend/app/registry_cache.py`) exposing typed reads
(`tools(exposed|full)`, `tools_by_ids`, `skill_by_id`, `sub_agent_cards`,
`sub_agent_snapshot`, `setting`, …). Its storage backend is selected by the
live `registry_cache_mode` setting and flippable at runtime (spec §7.3):

- **`bypass` (default)** — stateless; every read executes the same Postgres
  queries as before the layer existed. Byte-identical semantics. This is the
  shipped default and the no-degradation rollback lever: flipping back to
  `bypass` is an instant escape hatch from any cache bug.
- **`memory`** — per-process store, per-registry generation counters,
  reload-on-dirty: an invalidation marks the registry stale and the next
  read reloads it wholesale. Registries are small, and full reload can never
  leave a stale embedded relationship (skills embed tool rows, agent records
  embed skill names — dirtying a parent dirties dependents).
- **`redis`** — the same contract over Redis blobs (read-through,
  delete-on-invalidate) for future multi-replica deployments. `REDIS_URL`
  env-only; selecting the mode pings Redis and rejects the save if
  unreachable; optional compose profile.

**Invalidation is event-driven and exhaustive.** Every write path — registry
CRUD, status/exposure toggles, MCP ingest and `listChanged` re-ingest,
cascades, seed reload, settings PATCH — calls `invalidate(registry)` before
returning. **TTLs are forbidden**: an entry is either current or explicitly
invalidated, so the "next model call" freshness contract survives caching.
`GET /cache/status` and `POST /cache/refresh/*` exist as operator
visibility/override (§8.7), never as a correctness mechanism.

## Consequences

Positive:

- Zero-risk rollout: the default changes nothing, and the whole orchestrator
  test suite runs parametrized over cache modes to prove it.
- Mode flips are a live Settings toggle, not a deploy.
- One facade means the retrieval layer (ADR-0005) scores over a cache
  snapshot instead of issuing per-call queries.

Negative:

- The performance win is opt-in; a deployment that never flips to `memory`
  pays the facade indirection for nothing.
- Exhaustive invalidation is a discipline: any *new* write path must
  remember to call `invalidate()` — there is no TTL safety net by design.
- Three backends triple the storage-contract test surface; the redis backend
  is env-gated out of the default test run.

## References

- spec.md §7.3 (registry cache layer), §8.7 (cache controls in Settings)
- /home/user/concierge-agent/backend/app/registry_cache.py
- /home/user/concierge-agent/docs/acceptance/18-registry-cache-and-retrieval/
- Related: ADR-0003 (the middlewares that read through it), ADR-0008
  (cross-replica invalidation), ADR-0005 (retrieval over the snapshot)
