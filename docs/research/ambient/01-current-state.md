# 01 — Current state: what this codebase already gives ambient mode

**Date:** 2026-08-25 · **Branch:** `ambient_mode_exp` (from `dev` @ `e9522ab`, post-memory-merge)

Ambient mode = the agent doing useful, governed work **while the user is not
chatting**: watching for events, holding standing intents ("tell me when…"),
running routines, consolidating in the background, and deciding when — and
whether — to initiate contact. This inventory maps the platform against a
five-stage ambient pipeline: **trigger → decide → execute → deliver →
govern**. The verdict up front: *execute* and *govern* are largely built;
*trigger*, *decide*, and *deliver* are the new work.

## 1. Already built (promote, don't reinvent)

### Execution substrate

| Asset | Where | Ambient relevance |
|---|---|---|
| In-process periodic scheduler | `app/memory/lifecycle.py` `run_periodic_loop` (60s tick), jobs advisory-locked via `pg_try_advisory_lock` (classid 42016), per-job intervals, fail-open | **The** ambient job runner. Honors the no-broker constraint (spec §2); one replica works, N replicas safe. Decay/reflection/contradiction/compaction/mining already run on it — ambient watchers and routines are more rows in the same jobs table. |
| Post-run fire-and-forget chain | `app/memory/scheduler.py` (`on_run_completed` → digest → rollup → extract → procedural → citation) | Proven pattern for "work that follows an event without blocking it". An ambient trigger firing a run is the same shape in reverse. |
| Sleep-time compute, live | reflection (evidence-cited insights), fallback mining (INACTIVE `.skill.md` proposals), digest compaction | Ambient mode's "think while idle" half already exists — what's missing is the *event-facing* half. |
| Full run lifecycle headless | `create_run` / graph + agentic orchestrators run fine with no SSE client attached (the experiment harness proved hundreds of unattended runs incl. HITL pauses) | Ambient runs are ordinary runs; no new execution path needed. Missing only: provenance (what triggered this run). |
| Memory subsystem (M13–M18) | `app/memory/` | The ambient brain: pinned profile + preferences steer what's worth watching; semantic facts ground decisions; digests/rollups are ready-made "while you were away" material; procedural exemplars can learn which interventions get acted on (the vote lifecycle generalizes to intervention feedback). |

### Governance substrate

| Asset | Where | Ambient relevance |
|---|---|---|
| HITL gates | pause/approve/deny + form gates, queue UI | The approval channel for autonomous actions — an ambient run that reaches a risky step pauses exactly like an interactive one; the queue is already a surface the user visits. |
| Review-queue precedent | memory instruction quarantine (§16.2), mined-skill proposals (INACTIVE + review) | The "propose, never act" posture ambient suggestions need: machine writes that would change behavior land in a queue until a human approves. Same pattern, new object types. |
| Registry + exposure gates | tools/skills/sub-agents with `status` and `direct_exposure`; static-definition immutability | Watchers/routines should be registry citizens with the same discipline (an `ambient_exposure`-style gate deciding what may run unattended). |
| Runs ledger | `runs`/`run_steps`, §10 label set, per-step tokens | Complete audit trail for unattended work — the safety story starts here. Needs a `trigger` provenance column, not a new ledger. |
| Settings store | live-tunable, validated, dark-by-default precedent (`memory_enabled`) | `ambient_enabled=false` byte-identity is the obvious ship posture; budgets/quiet-hours are more validated keys. |
| Env-only secrets | `app/config.py` | Watcher credentials (IMAP, webhooks, API keys) follow the existing rule. |

### Plumbing that generalizes

- **LISTEN/NOTIFY** (§7.3 cache invalidation, `app/registry_cache.py`): an
  in-Postgres pub/sub channel already wired into the process — the natural
  spine for "event arrived → wake the decider" without any new infra.
- **MCP manager** (stdio/http, health checks, `listChanged`): the pluggable
  surface where event *sources* live — a watcher is naturally an MCP server
  (or a poll over one's tools), keeping third-party event access out of core.
- **SSE chat + activity events + A2UI**: the delivery surface for ambient
  output *inside* the app — an agent-initiated conversation message is a
  small delta on existing machinery.
- **Declarative seed documents** (`.skill.md` / `.agent.md` + doclint build
  gate): the authoring pattern for routines/standing intents as documents
  (`.routine.md`) with lint-time validation.
- **Idle-time experiment harness** (`experiments/`): the measurement culture
  — ambient behaviors (intervention precision, annoyance rate) get the same
  probe treatment memory got.

## 2. Gaps (the actual ambient work)

1. **No trigger substrate.** Nothing fires without a chat message: no cron
   expressions, no webhook receiver endpoint, no pollers, no event table.
   (`mcp_health_interval_s` polling is the only recurring outward look.)
2. **Idle detector is specced, not built.** `memory_idle_minutes` exists as a
   validated setting but *no code consumes it* — consolidation debounces 1s
   after each run instead of detecting conversation quiet. Ambient needs the
   real detector (spec §16.2 promised it "for watchers and standing intents").
3. **No standing-intent store.** "Tell me when X" has nowhere to live: no
   table for durable intents with their trigger condition, budget, expiry,
   and delivery preference.
4. **No decision layer.** Nothing answers "is this event worth a run?" or
   "is this result worth interrupting the user?" — the
   relevance/importance gate between triggers and runs, and between results
   and notifications, does not exist.
5. **No delivery channel beyond the open UI.** No push, no email, no digest
   builder; an ambient result today would sit in a conversation nobody has
   open. Interruption policy (push vs digest vs silent, quiet hours) is
   unmodeled.
6. **No ambient provenance or budgets.** Runs don't record what started them;
   there are no per-trigger/per-day token or action budgets, no runaway-loop
   breaker beyond the 1000-agent-style caps interactive runs have, and no
   autonomy levels (observe-only vs propose vs act-with-approval vs act).
7. **No user-absent identity semantics.** HITL assumes someone is watching;
   an ambient run pausing at a gate needs timeout semantics (expire, digest,
   or escalate) rather than waiting forever.

## 3. Design gravity from the constraints

- **Spec §2 stands**: one FastAPI process, asyncio, Postgres, three compose
  services — so triggers are (a) in-process schedules on the existing loop,
  (b) Postgres rows scanned by that loop, (c) LISTEN/NOTIFY wakeups, and
  (d) inbound webhooks on the existing FastAPI app. No Celery, ever.
- **Middleware precedence §7.0 stands**: ambient logic lives in schedulers,
  stores, and prompt assembly — not new middlewares.
- **Dark by default**: `ambient_enabled=false` must be byte-identical, same
  as memory's master switch, with the same experiment-harness proof.
- **The write gate is the security boundary** (memory's lesson): events are
  untrusted input; whatever an event says, it can only *propose* runs whose
  capabilities pass the same exposure/HITL gates as user-initiated work.
