# 05 — Ambient mode: full requirements

**Date:** 2026-08-25 · **Branch:** `ambient_mode_exp` · Grounded in docs 01–04;
gap research (heartbeats/CEP/presence) in doc 05b feeds the starred items.

## 0. Definitions and scope decisions (the framing questions, answered normatively)

**D1 — Ambient is a MODE, not an agent type.** Ambient mode changes the
*initiator* of work (a trigger instead of a chat message) and the *governance
envelope* (narrowed capability projection, budgets, autonomy ceiling,
delivery policy). It never changes the executor: the same registries, the
same two orchestrators, the same run/step ledger. A dedicated "ambient
operator" persona, if wanted, is a seeded sub-agent (registry record like
`memory-keeper`) — configuration, not architecture. *Rationale: a second
execution stack is the regression vector; reusing the first is the
non-regression guarantee.*

**D2 — Ambient work is NOT limited to memory consolidation.** Consolidation
(digest/extract/decay/reflect/compact) is one *job class* on the shared
scheduler. Ambient mode's primary product is **independent task execution**:
routines running any registry capability, standing intents watching external
sources, anticipation pre-computing likely briefings. Independence is
bounded by autonomy ceilings (D4), never by task domain.

**D3 — Heartbeats exist in three distinct senses**, specified separately:
- **H1 system tick**: the existing 60s advisory-locked loop — the substrate
  clock. Already built.
- **H2 agent-scheduled wakeups**: a native tool (`ambient.wakeup(delay,
  reason)`) letting a running agent schedule its own future continuation as a
  one-shot trigger — self-managed cadence for "check again in 20 minutes"
  work. Evidence: the best prospective-memory result on record (65.1% Set-F1)
  is specifically the heartbeat condition (PM-Bench).
- **H3 liveness watchdog**: lease/heartbeat bookkeeping on watchers and
  drains so a stalled or crashed background worker is *detected* and its
  routine auto-paused with a visible reason — silence must be
  distinguishable from failure.

**D4 — Autonomy ceilings**: `propose` (all consequential output queues for
review — default) and `act_reversible` (reversible actions execute;
irreversible classes always gate through HITL with absent-user timeout →
digest). No unattended-irreversible level exists.

**D5 — Dark by default.** `ambient_enabled=false` is byte-identical to the
pre-ambient platform, proven by the regression suite, same as memory.

## 1. Actors and canonical use cases

- **UC1 Morning routine**: "every weekday at 7:30, summarize what changed in
  the workspace and any open approvals" → scheduled routine → digest card.
- **UC2 Standing intent**: "tell me when the site notes mention line 3 again"
  → poll watcher + significance judge → interrupt or digest by urgency.
- **UC3 External push**: CI system POSTs to a routine's fire endpoint with a
  failure payload → triage → proposed fix run → approval in inbox.
- **UC4 Self-continuation**: a run hits a slow external process, calls
  `ambient.wakeup(20m, "re-check export job")`, ends cleanly, resumes later.
- **UC5 Absence work**: user idle 30 min → consolidation + anticipation jobs
  produce a "while you were away" briefing delivered at return-breakpoint.
- **UC6 Reminder w/ absence semantics**: "if the backup report hasn't landed
  by 18:00, flag it" → state/absence trigger (*CEP-lite, doc 05b*).
- **UC7 Platform reflex**: HITL item pending > timeout → expire gracefully,
  question rides the next digest.

## 2. Functional requirements

### FR-T — Trigger plane (taxonomy is closed and typed)

| id | Trigger type | Requirement |
|---|---|---|
| FR-T1 | Schedule | cron / fixed interval / one-shot at timestamp; stored UTC, entered local; per-routine stagger offset; one-shot auto-disables after firing |
| FR-T2 | Webhook fire | `POST /routines/{id}/fire`; per-routine bearer token (shown once, revocable, rotatable); payload stored verbatim, **always untrusted-fenced**; response returns run id |
| FR-T3 | Poller | cursor (last-seen id/timestamp) + lookback + early-termination over any MCP/native tool source; *adaptive cadence — backoff when quiet, tighten on activity (doc 05b)* |
| FR-T4 | Internal event | run completed/failed, HITL item aged, memory quarantined, MCP server health flip, registry change — emitted where they occur |
| FR-T5 | State condition | predicate over Postgres state evaluated on the tick (threshold crossed, count exceeded); **state ≠ event at the schema level** (TAP evidence) |
| FR-T6 | Presence | user-went-idle and user-returned events from the idle detector (FR-X6) |
| FR-T7 | Agent wakeup (H2) | `ambient.wakeup(delay_s, reason, context?)` native tool → one-shot trigger addressed to the calling routine/intent; capped count per routine per day |
| FR-T8 | Manual | "Run now" from UI/API, bypassing schedule but not governance |
| FR-T9 | NOTIFY | in-DB channel signals as wake pings only; jobs table is the source of truth, drained `FOR UPDATE SKIP LOCKED` |
| FR-T10 | MCP subscription | where a server declares `subscribe`, resource-updated notifications ingest as events (degrades to FR-T3 polling otherwise) |
| FR-T11* | Composite/absence | A-then-B-within-window and NOT-X-by-deadline patterns (*CEP-lite core per doc 05b*); **chaining depth-limited and cooldown-guarded; a routine's runs may not wake that same routine except via FR-T7** |

All events land in one append-only `ambient_events` table with source, trust
label, dedupe key, and verdict; ingestion never blocks the request path.

### FR-D — Decision plane

- FR-D1: three-tier gate — typed matchers (no model) → significance judge
  (one cheap structured call, only for semantic predicates) → run. No LLM
  call per raw event, ever.
- FR-D2: every decision persists a fire/hold record `{value, urgency,
  attention_state, decision, reason}`; the ledger is queryable from the UI.
- FR-D3: fail-open to **hold** (silence default); per-routine and global
  rate caps drop-and-count excess events.
- FR-D4: intervention precision (accepted / delivered) computed per category
  from delivery feedback; visible in UI; persistent low precision downgrades
  the category's channel automatically.

### FR-X — Execution plane

- FR-X1: routines are registry-grade records (trusted stored prompt, trigger
  specs, capability allowlist, model override, budgets, autonomy, status);
  definitions immutable for seeded/static routines except status toggles
  (registry discipline §4).
- FR-X2: a fire creates an ordinary Run with `trigger` provenance; both
  orchestrator modes supported; ambient runs never see the full catalog —
  only the routine's allowlist projection.
- FR-X3: every ambient prompt carries the abstain instruction; the abstained
  outcome is a first-class run result ("completed, did nothing, because…").
- FR-X4: budgets enforced by the runner (steps, tokens, wall-clock, external
  side-effects) + a tokens-without-progress monitor; breach ⇒ graceful stop
  + ledger reason.
- FR-X5: standing intents are typed rows (condition type, compiled rule,
  window, watermark, cadence, expiry, budget, delivery pref) created
  conversationally via `ambient.watch` or UI, with the compiled
  interpretation echoed back for confirmation before activation.
- FR-X6: the **real idle detector**: no active runs and no chat activity for
  `ambient_idle_minutes` (subsumes the unconsumed `memory_idle_minutes`) ⇒
  idle event; first activity after idle ⇒ return event.
- FR-X7: anticipation job (idle-time): predict likely next asks from
  episodic memory + intents, pre-compute briefing material, record per-item
  used/unused; self-prunes below a hit-rate floor.
- FR-X8: HITL in ambient runs pauses normally; pending > 
  `ambient_hitl_timeout_h` ⇒ run expires gracefully, the question rides the
  digest; resumable from the inbox while the checkpoint is retained.
- FR-X9 (H3): watchers and drains maintain lease/heartbeat rows; a lease
  stale beyond tolerance ⇒ watchdog pauses the routine with reason
  `stalled`, emits an internal event, and rescues the orphaned job (*lease
  intervals per doc 05b*).

### FR-V — Delivery plane

- FR-V1: all ambient output flows through a `deliveries` outbox — never
  directly to a surface.
- FR-V2: digest flush at configured times (default 2–3/day) AND at
  return-from-idle breakpoints; digest renders as one agent-authored
  conversation message + A2UI cards, grouped by category.
- FR-V3: urgency ≥ threshold bypasses to immediate delivery, debited from
  `ambient_notification_budget_per_day` (default 3); over budget ⇒ leads the
  next digest. Quiet hours absolute.
- FR-V4: accept/dismiss/ignore feedback captured per delivery item.
- FR-V5: pending approvals batch into the digest ranked by risk under a
  daily escalation budget.
- FR-V6: channels in scope: in-app conversation + inbox + toast. Email/push
  are post-M24 (interface reserved, not built).

### FR-G / FR-U / FR-A / FR-O — Governance, UI, API, Observability

- FR-G1: master switch + caps as validated settings; per-routine
  `consecutive_failures ≥ N` auto-pause with visible reason.
- FR-G2: run-status honesty — infrastructure success ≠ task success ≠
  did-something; three distinct ledger facets; "why did my routine do
  nothing?" answerable from ledger + fire/hold records.
- FR-G3: trust labels end-to-end (payloads untrusted, stored prompts
  trusted, digest content data-fenced); consolidation promotions screened
  for secrets/PII.
- FR-U1: §8.9 Ambient page — Routines / Watches / Inbox
  (Notify·Question·Review) / Ledger tabs, with fire-hold audit and precision
  sparklines.
- FR-A1: REST for routines (CRUD + fire + token lifecycle), intents,
  deliveries (feedback), events (read); all §10-labeled.
- FR-O1: tier `ambient` metrics/events for every plane transition; no
  payload content in logs — ids, counts, verdicts only.

## 3. Non-functional requirements

- **NFR-1 Non-regression (the headline)**: with `ambient_enabled=false`,
  every existing suite passes unchanged and the run path is byte-identical;
  with it on but zero routines/intents, chat behavior is unchanged except
  the idle detector's passive bookkeeping. Enforced by the same
  byte-identity test pattern memory used.
- **NFR-2 Single-process fidelity**: no broker, no new services; all planes
  live on the existing FastAPI process + Postgres (spec §2 unchanged).
- **NFR-3 Bounded cost**: worst-case ambient spend is computable from
  settings (caps × budgets); a runaway class must be impossible by
  construction (depth limits, cooldowns, no self-triggering except FR-T7's
  capped wakeups).
- **NFR-4 Crash resilience**: every trigger/job survives process restart
  (Postgres-backed); missed schedule ticks fire once on recovery, never
  replayed N times (watermark semantics); watchdog covers stalls.
- **NFR-5 Security**: fire tokens hashed at rest; payloads fenced; ambient
  runs cannot widen their own projection; secrets stay env-only.
- **NFR-6 Latency floors**: webhook-to-run under 5 s; scheduled fire within
  one tick + stagger; digest flush within a minute of its slot.
- **NFR-7 Auditability**: every user-visible ambient artifact traceable to
  event → decision → run → delivery rows.

## 4. Out of scope (POC)

Email/SMS/mobile push channels; location/device-context triggers;
multi-user attention arbitration; cross-instance ambient coordination
beyond advisory locks; learned (bandit) timing policies — the feedback
substrate (FR-V4) is built, the learner is post-M24.

## 5. Traceability

Every FR above cites into docs 02/03: trigger typing and event/state split
(03 §3, rules 6–8), no-LLM-wake (03 rule 2), silence default + precision
metric (03 rule 3), digest + bypass + budget (03 rules 4–5, 02 §8),
approval batching (03 rule 12), abstain + budgets (03 rule 13), untrusted
payloads + caps + stagger + auto-pause (02 §2.1, mech 8–9), watchdog and
heartbeat mechanics (05b), evaluation shape (03 rule 15).
