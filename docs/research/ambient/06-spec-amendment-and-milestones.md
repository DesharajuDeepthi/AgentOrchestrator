# 06 — Draft spec amendment (§17 Ambient Mode) and milestones M20–M24

**Date:** 2026-08-25 · **Branch:** `ambient_mode_exp` · Synthesizes docs 01–05b.
This is the text proposed to merge into `spec.md` (as §16 was merged from the
memory suite's doc 06), plus §3.7 settings, §8.9 UI, §14c acceptance steps,
and the milestone rows. **Nothing here is implemented yet — this document is
for sign-off.**

---

## Proposed spec §17 — Ambient Mode

### 17.0 Principles

Ambient mode is an **initiation-and-governance mode, not an agent type**: the
same registries, orchestrators, and run/step ledger execute all ambient work;
what changes is who starts a run (a trigger, not a chat message) and the
envelope it runs in (narrowed capability projection, budgets, autonomy
ceiling, delivery policy). Principles carried from §16: **dark by default**
(`ambient_enabled=false` ⇒ byte-identical); **registry citizenship** (ambient
tools/routines behind the same exposure discipline); **deterministic code at
the boundaries** (typed triggers, clamps, caps, timers) with the LLM only
inside framed judgments (significance, rule compilation, content); **the
action gate is the security boundary** (event payloads are untrusted input;
they may start runs, never steer them); **no new services**; **§7.0
middleware precedence untouched** (ambient logic lives in schedulers, stores,
and prompt assembly). Ambient work spans **any registry capability** —
consolidation is one job class, not the mode.

### 17.1 Storage

Tables (Alembic, one migration): `ambient_events` (append-only: kind, source
('schedule'|'webhook'|'poll'|'internal'|'wakeup'|'presence'|'pattern'|'manual'),
payload jsonb UNTRUSTED, dedupe_key, occurred_at, causation_id, correlation_id,
depth smallint default 0, verdict ('fired'|'held'|'expired'|'dropped'),
verdict_reason, routine_id?, intent_id?), `routines` (trusted prompt, triggers
jsonb, capability allowlist as registry refs, model_ref?, autonomy
('propose'|'act_reversible'), budgets jsonb, fire_token_hash, stagger_offset_s,
status, consecutive_failures, last_fired_at), `standing_intents` (text,
condition_type ('event'|'state'|'time'), compiled jsonb, semantic_predicate?,
window jsonb, watermark, cadence_s, current_interval_s + backoff columns,
expires_at, budget jsonb, delivery pref, status), `ambient_wakeups` (run_id?,
routine_id?, due_at, reason, payload jsonb, created_by
('agent'|'system'|'user'), status ('pending'|'fired'|'cancelled')),
`pattern_instances` (rule ref, partition_key, state
('armed'|'matched'|'expired'), a_event_id, deadline_at; unique armed instance
per rule+key), `deliveries` (run_id?, intent_id?, category, tier smallint
0=interrupt/1=notify/2=digest/3=silent, urgency 1-5, title, body,
deliver_no_later_than, delivered_at?, channel?, superseded_by?, feedback
('accepted'|'dismissed'|'ignored')?), `user_presence` (state
('active'|'idle'|'away'|'offline'), last_activity_at, last_heartbeat_at,
visible, updated_at). `runs` gains `trigger jsonb` provenance and
`last_heartbeat_at` (liveness). Invariants: events append-only; routine
definitions immutable for static seeds (status/exposure toggles only, §4
discipline); fire tokens stored hashed, shown once.

### 17.2 Trigger plane

The closed, typed taxonomy (doc 05 FR-T1..T11): schedules (cron/interval/
one-shot, stored UTC, per-routine stagger), webhook fires
(`POST /api/v1/routines/{id}/fire`, per-routine revocable bearer token,
payload always untrusted-fenced at prompt time), pollers (cursor + lookback +
early termination over MCP/native sources, **adaptive cadence**: on quiet
`current = min(current × 1.5, 3600s)`, reset to base 300s on activity;
near-due timers tighten the interval), internal platform events, state
conditions (evaluated on the tick; **state ≠ event at the schema level**),
presence events (idle/returned edges), **agent wakeups** (17.4), manual
fires, NOTIFY pings, MCP subscriptions where available, and composite
patterns (17.3a). Ingestion never blocks a request path; `NOTIFY` is a
wake-up ping only — the drain reads `ambient_events` with `FOR UPDATE SKIP
LOCKED`; the 60s tick sweeps missed pings.

### 17.3 Decision plane

Three tiers strictly ordered by cost: (1) typed matchers — field operators
(equals/contains/starts_with/one_of/regex), event-vs-state semantics, dedupe,
rate caps (per-routine hourly; excess **dropped and counted**, never queued);
(2) significance judge — one structured-output call, defaulting to the
extraction-model role with a per-intent model override for high-stakes
watches; returns
`{significant, urgency 1-5, reason}`; failure ⇒ **held** (silence default);
(3) the run. Every decision writes a fire/hold record `{value, urgency,
attention_state, decision, reason}`. **No LLM call per raw event, ever**
(evidence: doc 03 rule 2). Intervention precision per category is computed
from delivery feedback and surfaces in the UI; persistently low precision
auto-downgrades the category one tier.

#### 17.3a Composite patterns (CEP-lite)

Exactly three composite kinds — `sequence` (A then B within T), `conjunction`
(A and B within T), `absence` (A without B by T) — as `pattern_instances`
keyed by partition. **Absence is a timer, not a query**: arming inserts an
instance with `deadline_at`; the tick fires expired armed instances (≤60s
slop). Chaining guards, all four: derived events carry
`causation_id/correlation_id/depth`; `depth ≥ 4` rejected; a rule never fires
if its own id appears in its causation chain (**no self-trigger**); kill
switch at 50 fires/rule/hour (auto-disable + ledger + notification); per
(rule, partition) cooldown default 300s. Routines may not wake themselves
except via the capped wakeup tool.

### 17.4 Execution plane

A fire creates an **ordinary run** (either orchestrator mode) with `trigger`
provenance, the routine's narrowed registry projection, per-run budgets
(max steps/tokens/wall-clock/side-effects) enforced by the runner, a
tokens-without-progress monitor, and the **abstain instruction** — the
abstained outcome is a first-class result. Autonomy: `propose` (default —
consequential output queues for review) and `act_reversible` (irreversible
action classes still gate). Ambient HITL pauses expire gracefully after
`ambient_hitl_timeout_h` — the question rides the digest, the checkpoint
stays resumable from the inbox.

**Heartbeats, three senses** (doc 05 D3): the 60s advisory-locked tick (H1,
exists); **agent-scheduled wakeups** (H2) via native tools
`ambient.wakeup(delay_or_at, reason)` and `ambient.cancel_wakeup(id)` —
platform clamps delays to [60s, 24h], caps 5 pending + 100/day per routine,
and applies a done-guard at fire time (the routine's last run superseding the
reason ⇒ wakeup expires); on tool failure inside an ambient run the runner
schedules one immediate self-wake with the error in context (Letta pattern)
instead of dying silently; **liveness watchdog** (H3) — ambient runs and
watchers refresh `last_heartbeat_at` each tick; the reaper marks rows stale
past 5 min (3–5× cadence) as `stalled`, auto-pauses the owning routine with a
visible reason, and rescues or fails the orphaned work.

**Standing intents** compile once at creation (NL → typed rule via the LLM,
interpretation echoed back for confirmation) and are evaluated by the
scheduler on their cadence — never remembered in model context (doc 03
rule 6). **Idle work**: the real idle detector (no active runs + no chat for
`ambient_idle_minutes`) triggers consolidation, plus the anticipation job —
predict likely next asks from episodic memory + intents, pre-compute
briefing material, record per-item used/unused, self-prune below the
hit-rate floor.

### 17.5 Delivery plane

All ambient output flows through the `deliveries` outbox with four tiers:
**0 interrupt** (immediate regardless of presence; reserved for run-blocking
gates and hard failures; debited from `ambient_notification_budget_per_day`,
default 3; over budget ⇒ leads next digest), **1 notify** (flush on the next
user-returned edge, bounded deferral `deliver_no_later_than = created + 30
min` enforced by the tick — Horvitz bounded deferral), **2 digest** (default;
flushed at `ambient_digest_times`, default 2/day, and on return from absence
> 1h as one collapsed "while you were away" card stack; micro-absences < 5
min flush tier 1 only), **3 silent** (ledger only — silence is an explicit,
logged decision). `superseded_by` collapses stale items. Quiet hours
absolute. Agents must justify tier ≤ 1 in the run record; the default is
digest (the Gmail-nudge bias: conservative keeps trust). Presence comes from
the client (visibility + throttled activity + 30s heartbeat with immediate
beat on foreground): active / idle (5–30 min) / away / offline (>30 min);
the away→active edge emits `user_returned` into the event stream.
Approvals batch into the digest ranked by risk under a daily escalation
budget. Feedback (accepted/dismissed/ignored) is captured per item.

### 17.6 Governance, observability, evaluation

Master switch + caps as validated settings (below); `consecutive_failures ≥
3` auto-pauses a routine with reason; run-status honesty distinguishes
infrastructure success / task success / did-something, and "why did my
routine do nothing?" resolves from ledger + fire/hold records. Trust labels
end-to-end; consolidation promotions screened for secrets/PII. §10 gains
tier `ambient`, kinds {ingest, match, judge, fire, hold, wakeup, pattern,
deliver, digest, expire, stall, pause}; counters and histograms per plane;
no payload content in logs. §11 gains: byte-identity with ambient off,
clamp/cap/guard enforcement, absence-timer semantics, watchdog rescue,
tier/budget delivery policy, untrusted-payload fencing. Evaluation: a
simulated-clock scripted-event harness (`experiments/ambient/`) scoring
fire/hold Set-F1 with false-alarm accounting, reaction time, and token cost
per configuration, plus a live multi-day soak.

### 17.7 Adaptive policy learning (M25)

The delivery feedback substrate (accepted/dismissed/ignored per item, doc 05
FR-V4) feeds a bandit-style learner that proposes policy adjustments:
shifting `ambient_digest_times` toward observed acceptance windows,
re-tiering categories (a chronically dismissed interrupt category down; a
consistently accepted digest category up), and tuning per-intent judge
thresholds. Governed by `ambient_learning_mode`: `off` (default — collect
only, dark-by-default discipline), `auto` (**the primary mode**: adjustments
apply immediately with no approval step, within hard clamps — digest times
move ≤ 2h from configured, tiers move one step at a time, never into tier 0
— every change ledgered and one-click revertible), `propose` (optional
cautious mode: each adjustment lands in the review queue and activates on
approval — the §16.2 quarantine pattern). Both modes are fully implemented;
auto is not gated behind propose. The reward
is a blend: acceptance + downstream usefulness (the delivered item's run/
memory was later referenced) − explicit-dismissal penalty, with a
repetition-decay term (recovering-bandit shape); pure acceptance optimization
is forbidden by construction. Learner runs as a consolidation-class job;
byte-identical when `off`.

---

## §3.7 settings additions

`ambient_enabled (default false)`, `ambient_max_routines (10)`,
`ambient_runs_per_day (50)`, `ambient_routine_events_per_hour (20)`,
`ambient_idle_minutes (10 — subsumes memory_idle_minutes)`,
`ambient_hitl_timeout_h (24)`, `ambient_digest_times (["09:00","17:00"]
local)`, `ambient_notification_budget_per_day (3)`, `ambient_quiet_hours
(["22:00","07:00"])`, `ambient_interrupt_threshold (4 — urgency ≥ this may
use tier 0/1)`, `ambient_wakeups_per_routine_per_day (100)`,
`ambient_escalation_budget_per_day (10)`, `ambient_learning_mode ('off'|'propose'|'auto', default 'off', §17.7)`.

## §8.9 Ambient page (UI)

Four tabs: **Routines** (CRUD, trigger editor with the filter-operator set,
fire-token lifecycle, per-routine run history + auto-pause reasons),
**Watches** (standing intents: original text + compiled rule echo, watermark,
cadence/backoff state, expiry), **Inbox** (Notify · Question · Review items,
digest preview, approval batch ranked by risk), **Ledger** (fire/hold audit
with reasons, correlation-chain view for patterns, intervention-precision
sparkline per category). Chat composer gains nothing — ambient never changes
the interactive surface when dark.

## §14c acceptance additions (run with `ambient_enabled=true`)

20. Create a scheduled routine in the UI; observe the staggered fire, the
    run with trigger provenance, and the digest delivery.
21. Fire a routine via `curl` with its bearer token and an adversarial
    payload ("ignore your instructions…"); verify the run starts and the
    payload is fenced and not obeyed.
22. Create a standing intent conversationally; verify the compiled-rule
    echo, then plant a matching event and a non-matching event; exactly one
    fire.
23. Absence pattern: arm "if X doesn't arrive by T"; verify the timer fires
    within one tick of T.
24. Agent wakeup: a routine run schedules its own re-check; verify clamp,
    the done-guard, and cancellation.
25. Kill a watcher mid-poll; verify the watchdog stalls→pauses it with a
    visible reason and the orphaned job is rescued.
26. Urgency 5 event during quiet hours vs outside; verify budget debit,
    quiet-hour suppression to digest-lead.
27. `ambient_enabled=false`: byte-identity regression suite passes.
28. (M25) With `ambient_learning_mode='auto'`, dismiss a category three
    times; verify the clamped re-tier applies WITHOUT approval, with a ledger
    entry and a working revert control; with `'propose'`, the same signal
    produces a queued proposal that applies only on approval.

## Milestones (proposed)

| # | Deliverable | Measured proof |
|---|---|---|
| M20 | Ambient substrate: all tables + migration, settings, webhook endpoint + token lifecycle, NOTIFY-wake drain, **real idle detector + presence states**, run `trigger`/`last_heartbeat_at`, byte-identity when dark | §14c-27; curl-able fire |
| M21 | Trigger + decision planes: schedules with stagger, adaptive pollers, state conditions, internal events, three-tier gate + fire/hold ledger, caps/drops/auto-pause, **CEP-lite (sequence/conjunction/absence) with all four chaining guards** | scripted-event harness: tier precision, absence-timer slop, cascade guards |
| M22 | Execution plane: routines end-to-end (both orchestrators, narrowed projection, budgets, abstain, progress monitor), standing intents (compile-echo-confirm → evaluate → fire), **agent wakeups with clamps/caps/done-guard**, watchdog + orphan rescue, HITL timeout semantics | §14c-20..25 |
| M23 | Delivery plane + §8.9 UI: outbox tiers, digest builder + return-flush + supersede-collapse, budgets/quiet hours, approval batching, **feedback capture + reward computation substrate** (blended reward persisted per delivery), rule-based precision auto-downgrade; anticipation job with hit-rate metric | §14c-26 + stage-3x UI evidence campaign |
| M24 | Ambient evals: simulated-clock event harness (fire/hold Set-F1 + false alarms, reaction time, cost) across gate ablations and delivery policies incl. cascade stress; multi-day live soak; experiment report | 07-style results doc |
| M25 | Adaptive policy learning (§17.7): bandit learner over the M23 reward substrate — digest-time shifting + category auto-tiering, **auto mode first-class (no approval), propose mode optional**, clamps, ledgered revertible changes; measured against static policy on the M24 harness | learning-on ≥ static policy on intervention precision; zero learner-caused tier-0 escalations |

## Settled decisions (from docs 02–05b) and open questions

**Settled**: mode-not-agent-type; the 11-type closed trigger taxonomy;
no-LLM-wake; standing intents as rows with compile-echo-confirm; three
heartbeat senses with clamp/caps/done-guard/cancel; absence as armed timers;
four chaining guards with depth 4 / cooldown 300s / 50-per-hour kill switch;
four delivery tiers with digest default, bounded deferral, and
justify-to-interrupt; auto-pause + run-status honesty; dark-by-default
byte-identity.

**Decided at sign-off (2026-08-25)**: (1) **in-app channels only** for
M20–M24 — the outbox `channel` field reserves the email/push seam, no new
infra; (2) **routine→routine chaining allowed from M21**, with all four
cascade guards mandatory from day one (depth ≤ 4, no-self-trigger via the
causation chain, 50 fires/rule/hour kill switch, 300s cooldowns) — guards
unit-tested in M21, stress-proven on the M24 harness; (3) significance judge
defaults to the **extraction role with a per-intent model override**;
(4) **learned timing policies get their own milestone M25**: a bandit-style
learner over delivery feedback (digest timing shifts, category re-tiering)
with `ambient_learning_mode` ∈ {'off' (default), 'propose' (adjustments land
in the review queue for approval — the memory-quarantine pattern), 'auto'
(applies within clamps, every change logged and revertible)}. The reward
blends acceptance with downstream usefulness and dismissal penalties —
acceptance-only rewards are the documented trap (doc 03 rule 14).
