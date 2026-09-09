# 04 — Ambient mode: architecture proposal

**Date:** 2026-08-25 · **Branch:** `ambient_mode_exp` · Grounded in docs 01–03.

Ambient mode is a **pipeline of five planes** over machinery the platform
largely already has (doc 01): **trigger → decide → execute → deliver →
govern**. The design principles carry over from memory verbatim: dark by
default (`ambient_enabled=false` ⇒ byte-identical), registry citizenship,
deterministic code at the boundaries with the LLM only inside well-framed
judgments, the write/action gate as the security boundary, no new services,
and §7.0's middleware precedence untouched.

The two decisive research findings shaping everything below:
1. **The wake decision must not be an LLM call per event** — cheap typed
   triggers beat LLM wake-judges by +16.7 F1 at 12–83× lower cost (doc 03
   rule 2), and frontier models cap at ~66% F1 on fire/hold even fine-tuned.
2. **Standing intents must not live in model context** — prospective memory
   decays to 16.7% hit rates on non-clock monitoring (doc 03 rule 6). They
   are Postgres rows; the scheduler owns *when*, the LLM owns only *what*.

## A1 — Trigger plane

**`ambient_events`** (append-only): `id, source ('schedule'|'webhook'|'poll'|'internal'), kind, routine_id?, intent_id?, payload jsonb (UNTRUSTED), dedupe_key, occurred_at, received_at, processed_at, verdict ('fired'|'held'|'expired'), verdict_reason`.

Ingestion paths — all four fit the existing single process:
- **Schedules**: the existing periodic loop (60s tick) evaluates routine cron
  specs (stored UTC, entered local; per-routine consistent stagger offset —
  doc 02 mech 9) and inserts a schedule event.
- **Webhooks**: `POST /api/v1/routines/{id}/fire` on the existing FastAPI
  app, guarded by a per-routine bearer token (shown once, revocable). The
  payload is stored verbatim and *always rendered inside an untrusted fence*
  — the Claude Routines discipline (doc 02 §2.1): a leaked token can start a
  run, never steer one.
- **Pollers**: watcher jobs on the periodic loop call MCP/native tools with a
  stored cursor (last-seen id/timestamp + lookback, early termination — the
  EAIA shape) and insert one event per new item.
- **Internal**: run completions, HITL items aging out, memory quarantines —
  emitted where they happen, giving ambient reflexes over the platform itself.

Wake-up: `NOTIFY ambient_events, '<id>'` as a **ping only** — the drain reads
the table with `FOR UPDATE SKIP LOCKED`; the periodic sweep covers missed
pings (doc 02 §6.3: jobs table is the truth, NOTIFY is the doorbell).

## A2 — Decision plane (the wake gate)

Three tiers, strictly ordered by cost (doc 03 rules 2–3):
1. **Typed matchers (no model)**: SQL predicates over the event — source and
   kind filters, field operators (equals/contains/regex — the Claude Routines
   filter set), **event-vs-state distinguished at the schema level** (the
   dominant TAP bug class), rate caps (per-routine hourly, per-day), dedupe.
   Optional pgvector similarity against the intent's predicate embedding as a
   prefilter.
2. **Significance judge (one cheap call)**: only for events that pass tier 1
   *and* whose intent carries a semantic predicate ("worth telling me about"),
   a single structured-output call on the extraction-model role: `significant:
   bool, urgency: 1-5, reason`. Fail-open to **held** (silence is the default).
3. **Full run**: only on fire.

Every decision writes a **fire/hold record** (`value_estimate, urgency,
attention_state, decision, reason`) — Horvitz's expected-utility calculus
made auditable, and the substrate for the intervention-precision metric.

## A3 — Execution plane

**`routines`** (registry-grade records, UI-managed): `id, name, prompt
(TRUSTED — stored by an authorized session), triggers jsonb, skill/tool
allowlist (a narrowed registry projection — ambient runs never see the full
catalog), model_ref?, autonomy ('propose'|'act_reversible'), budgets
(max_steps, max_tokens, max_wall_clock_s, max_side_effects), status, stagger_offset_s,
last_fired_at, consecutive_failures`.

A fire creates an **ordinary Run** — same orchestrators, same ledger — with
`trigger jsonb` provenance (`{routine_id, event_id, source}`) and
`orchestrator_mode` unchanged; the run executes under the routine's narrowed
projection and budgets, its prompt always includes the **abstain
instruction** ("if the event needs nothing, say so and stop" — +0.39 safety
for −0.03 helpfulness, doc 03 rule 13). Autonomy semantics: `propose` — any
consequential action pauses at the existing HITL gate; `act_reversible` —
reversible actions run, irreversible classes still gate. **HITL timeout for
absent users**: a gate pending > `ambient_hitl_timeout_h` expires the run
gracefully and lands the question in the digest instead of blocking forever.

**`standing_intents`**: `id, text (user's words), condition_type
('event'|'state'|'time'), compiled jsonb (typed rule — compiled by the LLM
once at creation and echoed back for confirmation), semantic_predicate?,
window jsonb, watermark, cadence_s, expires_at, budget jsonb, delivery
('digest'|'interrupt'|'auto'), status`. Creation is conversational (a native
tool `ambient.watch` + UI form), but the stored object is the typed row.

**Anticipation (idle work)**: the real idle detector (finally consuming
`memory_idle_minutes`: no active runs and no chat for N minutes) triggers,
besides consolidation, an **anticipation job**: predict likely next asks from
episodic digests + standing intents, pre-compute briefing material into the
memory store, and record per-item whether it was ever used — anticipation
prunes itself where hit-rate is low (doc 03 rule 9). Plan reuse for routine
runs falls out of the existing procedural exemplars (plan-cache literature,
doc 03 rule 10) — no new machinery.

## A4 — Delivery plane

**`deliveries`** (outbox): `id, run_id?, intent_id?, category, urgency 1-5,
title, body, created_at, delivered_at?, channel?, feedback
('accepted'|'dismissed'|'ignored')?`.

Policy (deterministic, settings-driven):
- **Digest by default**: flushed at `ambient_digest_times` (default 2–3/day,
  predictable — the N=237 RCT result) *and* opportunistically at breakpoints
  (user returns from idle / finishes a turn-burst — Attelia's −33–46% load).
  The digest is one agent-authored conversation message + A2UI card stack.
- **Interrupt bypass**: `urgency ≥ ambient_interrupt_threshold` delivers
  immediately (UI toast + conversation), debited from a hard
  `ambient_notification_budget_per_day` (default 3); over-budget urgent items
  still lead the next digest. `ambient_quiet_hours` is absolute.
- **Approvals batched**: pending low-risk HITL items ride the digest, ranked
  by risk, under a per-day escalation budget — approval is a fatiguing
  resource (inverted-U, doc 03 rule 12).
- **Feedback is first-class**: accept/dismiss/ignore captured per delivery;
  rolling intervention precision per category surfaces in the UI, and a
  category muted/dismissed persistently gets auto-downgraded a channel.

## A5 — Governance

- `ambient_enabled=false` default; byte-identity regression in the suite.
- Hard caps as settings: max active routines (default 10), runs/day,
  per-routine hourly event caps (excess **dropped and counted**, not queued).
- Runaway containment: per-run budgets enforced in the runner, plus a
  tokens-without-progress monitor on ambient runs; `consecutive_failures ≥ N`
  auto-pauses a routine (auto-pause precedent: every shipped system).
- **Run-status honesty**: ambient run rows distinguish "completed" from
  "did anything" (the abstained/no-op outcome is explicit); a "why did my
  routine do nothing?" ask resolves against the ledger + fire/hold records.
- Trust labels: event payloads untrusted, routine prompts trusted, digest
  content data-fenced like memory blocks. Consolidation promotions get a
  PII/secret screen (memory-risk grows longitudinally, doc 03 rule 15).
- Every plane emits §10-labeled events/metrics: tier `ambient`, kinds
  {ingest, match, judge, fire, hold, deliver, digest, expire, pause}.

## UI (§8.9 Ambient page)

Four tabs: **Routines** (CRUD, trigger editor with filter operators, token
management, per-routine run history), **Watches** (standing intents with
compiled-rule echo, watermark, expiry), **Inbox** (LangChain's
Notify/Question/Review vocabulary — pending questions/approvals + digest
preview), **Ledger** (fire/hold audit with reasons, intervention-precision
sparkline per category).

## Measured goals (the experiment, `experiments/ambient/`)

SentinelBench-shaped harness: a **scripted event sequence on a simulated
clock** (time-warp, like the memory long-horizon sim) with planted
significant and insignificant events, scoring **fire/hold Set-F1 with
false-alarm accounting, reaction time, and token cost** per configuration —
tiers 1–2 ablated, digest-vs-interrupt policies compared, byte-identity when
off. Plus a live multi-day soak on the real stack.

## Proposed milestones

| # | Deliverable | Proof |
|---|---|---|
| M20 | Ambient substrate: events/routines/standing_intents/deliveries tables + migration, master switch + settings, webhook endpoint + bearer tokens, NOTIFY-wake drain on the existing loop, **real idle detector**, run `trigger` provenance | dark-by-default byte-identity; curl-able fire |
| M21 | Trigger + decision plane: cron routines with stagger, pollers with cursors, three-tier wake gate, fire/hold ledger, caps + auto-pause | scripted-event harness: tier-1/2 precision |
| M22 | Standing intents end-to-end: `ambient.watch` tool + NL→typed compile with echo-back, evaluation on cadence, expiry | "tell me when X" fires once, correctly, on the harness |
| M23 | Delivery plane + Ambient UI: digest builder + breakpoint flush, interrupt bypass + budgets + quiet hours, approval batching + HITL timeout, feedback capture, §8.9 page | stage-3x UI evidence campaign |
| M24 | Ambient evals + anticipation: simulated-clock experiment matrix (intervention precision / reaction / cost), anticipation job with hit-rate metric, multi-day soak | experiment report 07-style |

## Settled decisions & open questions

**Settled** (evidence in docs 02–03): typed triggers before any model call;
standing intents as rows; digest-default with urgency bypass; abstain +
budgets on every ambient run; untrusted fire payloads; caps + stagger +
auto-pause; fire/hold auditability; silence as default posture.

**Open for sign-off**: (1) delivery channels beyond the UI (email/push) — 
propose deferring to post-M24; (2) whether routines may *chain* (a routine's
run emitting events that wake other routines) — propose forbidding until the
eval suite exists (runaway-loop class); (3) autonomy default for seeded
routines — propose `propose` (L2) everywhere initially.
