# ADR-0008: Postgres LISTEN/NOTIFY for cross-replica cache invalidation

Status: Accepted

Date: 2026-08-07

## Context

The registry cache (ADR-0004) is event-invalidated: every write path
invalidates before returning. That is airtight in one process. The moment a
second backend replica exists, a write handled by replica A must also dirty
replica B's `memory` cache, or B serves stale catalogs until its own next
write. The conventional answer is Redis pub/sub — but Redis is deliberately
optional in this architecture (ADR-0001), and cache-mode `memory` must work
on the default three-service stack. Postgres, the one required stateful
service, already ships a pub/sub primitive: LISTEN/NOTIFY.

## Decision

Cross-replica invalidation rides Postgres (`backend/app/registry_cache.py`):

- **Broadcast**: `RegistryCache.invalidate()` first marks the local cache
  dirty, then fires `pg_notify` on the shared channel
  `registry_cache_inv` with an **origin-tagged payload**
  (`"{origin}:{registry}"`, origin being a per-process `uuid4().hex`).
- **Listen**: each process holds a dedicated `asyncpg` connection with a
  listener on that channel. On notification it drops own-origin payloads
  (`origin == self._origin`) and otherwise schedules `_mark_dirty(registry)`
  — the local-only path with relationship propagation (tools → skills →
  sub_agents), which never re-notifies. Loops are therefore **impossible by
  construction**: the notify entry point and the listener entry point are
  different methods, and only the former broadcasts.
- **Best-effort, dormant on one node**: single-replica correctness never
  depends on the notify path — the writing process already marked itself
  dirty before broadcasting. Notify failure and listener unavailability log
  a warning and change nothing else. On a single node the machinery idles.
- Redis pub/sub was rejected for this role because it would make Redis
  required infrastructure for a multi-replica `memory` deployment; the
  chosen design adds **zero** services. (`quick-setup.sh --redis` can still
  provision the optional Redis cache backend; that is orthogonal.)

## Consequences

Positive:

- Multi-replica readiness with no new infrastructure and no behavior change
  on the shipped single-node stack.
- The origin filter plus notify/mark-dirty asymmetry makes invalidation
  storms and echo loops structurally impossible, not just unlikely.
- Payloads carry only registry names — no data on the channel, nothing
  sensitive to leak, no payload-size concerns (NOTIFY caps at 8000 bytes).

Negative:

- Best-effort delivery: NOTIFY is fire-and-forget per connection — a replica
  whose listener connection has dropped misses invalidations until it
  reconnects; there is no replay. Acceptable only because staleness in
  `memory` mode heals on the replica's own next write or manual refresh,
  and `bypass` remains the correctness fallback.
- A dedicated asyncpg connection per process is one more long-lived
  connection to babysit (and a second DSN parse, since the SQLAlchemy URL
  must be rewritten for asyncpg).
- LISTEN/NOTIFY does not cross Postgres instances; a future multi-database
  topology would need a real bus.

## References

- spec.md §7.3 ("Cross-replica sync (ready, dormant on one node)")
- /home/user/concierge-agent/backend/app/registry_cache.py
  (`_notify_peers`, `start_listener`, `_mark_dirty`, `_NOTIFY_CHANNEL`)
- Commit `c8269c5` — feat(cache): LISTEN/NOTIFY cross-replica invalidation
- Related: ADR-0001 (no required infra beyond Postgres), ADR-0004 (the
  cache being invalidated)
