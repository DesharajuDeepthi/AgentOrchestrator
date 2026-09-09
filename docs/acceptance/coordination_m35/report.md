# M35 acceptance — multi-replica ambient coordination (spec §18.9)

## In-process proof (the spec's correctness gate)

`tests/test_m35_coordination.py` — 7 tests, green in the full suite
(691 passed, 1 skipped):

- the lease: second `LeaderLease` blocked while the first holds; renewal
  keeps leadership; a KILLED session (conn closed without unlock — the crash
  case) lapses the lock and the other lease acquires, while the dead one
  reports `False` and cannot silently retake;
- two concurrent `run_ambient_loop`s over the same Postgres: the evaluator
  probe fires on exactly ONE loop while the drain probe fires on BOTH
  (non-leaders LISTEN + drain — the SKIP-LOCKED drain and executor are
  replica-safe by construction);
- stopping the leader: the follower's evaluator probe takes over within one
  tick;
- a dark loop (ambient off) holds no lease at all.

## Live two-replica proof (real stack, tick_s=60)

The compose backend (M35 image) plus a second container from the same image
on the same network + database (no published port — "any port mapping the
operator chooses"):

| time (UTC) | action | observation |
|---|---|---|
| 00:33 | backend-1 boots | `ambient_leader_acquired` in its log; `/metrics` → `concierge_ambient_leader 1.0` |
| 00:35 | replica2 boots | replica2 gauge `0.0`, zero acquisition logs — exactly one leader |
| 00:36:23 | `docker stop` backend-1 (the leader) | — |
| 00:37:11 | replica2 logs `ambient_leader_acquired` | takeover in **48s ≤ one 60s tick**; replica2 gauge `1.0` |
| 00:38 | backend-1 restarted | comes back gauge `0.0` while replica2 still leads — the lease is exclusive |
| 00:38:35 | `docker stop` replica2 | — |
| 00:39:26 | backend-1 re-acquires | gauge back to `1.0` in **51s ≤ one tick** |

Mechanism: one Postgres SESSION advisory lock on the dedicated pair
`(427017, 1)` held on a dedicated unpooled connection — the session IS the
lease. Renewal is a per-tick liveness check of that connection; process
death releases the lock server-side, so failover needs no lease table and
no clock comparison. A clean stop releases the lock immediately; ambient
going dark surrenders it too. Compose stays three services; registry-cache
invalidation already rides LISTEN/NOTIFY (M8b).
