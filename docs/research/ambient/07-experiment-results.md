# Ambient mode — experiment results (M24)

Spec §17.6 called for a simulated-clock scripted-event harness scoring
fire/hold Set-F1 with false-alarm accounting, reaction time, and cost per
configuration, plus a live soak. Harness: `experiments/ambient/simharness.py`
(scenario battery + guard battery) and `experiments/ambient/soak.py`
(simulated 3-day soak + compressed live soak). All results in
`experiments/ambient/result_*.json`.

## 1. Scenario battery — decision-plane quality

Six scripted scenarios cover the trigger taxonomy: a 5-event noise storm
against an incident watch, a real incident (+ a benign postmortem follow-up),
an absence timer that must fire (backup started, never finished), an absence
that must stay silent (backup finished in time), a deploy→alert sequence
pattern, and an interval schedule. Ground truth is per event; scoring is set
precision/recall/F1 over fired labels, with false alarms counted separately.
Reaction time is measured in 60s ticks from the moment a derived fire became
due (absence deadline, pattern completion, schedule slot).

| config | judge | Set-P | Set-R | Set-F1 | false alarms | max reaction (ticks) | wall |
|---|---|---|---|---|---|---|---|
| `tier1_only` | none (typed filters only) | 0.40 | 1.00 | 0.57 | **6** | 1 | 1.7 s |
| `judge_fake` | fake:scripted | 1.00 | 1.00 | **1.00** | 0 | 1 | 1.5 s |
| `judge_live` | **openrouter:qwen/qwen3.8-max** (effort low) | 1.00 | 1.00 | **1.00** | 0 | 1 | 43.0 s |

Findings:

- **The tier-2 judge is what buys precision.** Typed filters alone recall
  everything but fire on all five noise items plus the benign postmortem —
  six false alarms across a 7-signal battery. Exactly the doc-03 result the
  three-tier design predicted: filters bound the candidate set, judgment
  prunes it.
- **The live judge matched ground truth perfectly.** Qwen 3.8 Max at low
  effort held "deploy completed, all probes green", "latency back to
  baseline", and "post-incident review scheduled" — the deliberately
  tempting near-misses — and fired only the customers-cannot-pay incident.
  One structured call per candidate (7 judge calls total for the battery),
  ~6 s per call wall-clock in this run.
- **Timers behave.** The absence timer fired within one tick of its
  deadline; the satisfied absence stayed silent; the sequence pattern fired
  on B-after-A only. (This battery caught a real defect before it shipped:
  pattern filters saw only payload fields while event-matched watches also
  saw `kind`/`source` — the data models are now unified, with a regression
  test.)

## 2. Guard battery (`result_cascade.json`) — cascade stress

The numbers that must hold regardless of what any model says:

| guard | stress | result |
|---|---|---|
| dedupe | 50 identical `dedupe_key` emits | **1 row stored** |
| chain depth | 10-deep derivation chain | **rejected at depth 4** (max stored depth 3) |
| kill switch | 60 routine-addressed events in one hour | **50 accepted, then hard-stop** |

## 3. Simulated 3-day soak (`result_soak_sim.json`)

72 hours of simulated time in 28 s of wall clock (5-minute steps): a daily
09:00 cron routine, an AIMD poller over a quiet→burst→quiet feed, and two
digest items arriving each simulated day.

- **Schedules**: 4 fires — one catch-up on the first tick (the 09:00 slot
  "missed" before the sim started fires exactly once, never N times), then
  one per day. Watermark semantics as specified.
- **Adaptive polling**: 80 checks instead of the 864 a fixed 5-minute
  cadence would have cost (**10.8× cheaper**). The interval backed off
  300 s → 3600 s through the quiet day, the single burst item was caught on
  the next due check and reset the interval to base, then it backed off
  again. Final interval: 3600 s.
- **Digests**: exactly **6 batches = 2/day** at the configured 09:30/17:30,
  none during quiet hours.

## 4. Compressed live soak (`result_soak_live.json`)

A 60s-interval routine ran unattended against the live stack
(`default_model=openrouter:qwen/qwen3.8-max`, all roles) for 8 minutes —
real fires, real runs, real deliveries:

| metric | result |
|---|---|
| interval slots elapsed | 8 |
| events fired | **8/8** (one per minute slot, zero duplicates) |
| runs completed | **8/8** (`stalled` = 0, `failed` = 0) |
| outbox rows | 8 — **7 superseded by their successor**, 1 pending |

The supersede-collapse (spec §17.5) did exactly its job in the wild: each
new heartbeat report replaced the still-undelivered previous one, so eight
runs would reach the user as ONE digest card, not eight. One observed
limitation: the routine's "abstain if nothing new since the previous beat"
instruction never triggered, because each ambient run starts a fresh
conversation with no memory of the prior beat — cross-fire context is
memory-layer (§16) territory (`include_memories` for ambient runs is a
deliberate M25+ candidate), not a delivery-plane defect.

A true multi-day live soak does not fit in a working session; the 3-day
portion ran on the simulated clock above, and the compressed live soak
validates the same loop end-to-end on real infrastructure. This is stated
plainly rather than claimed otherwise.

## 5. What M25 inherits

- The reward substrate is populated by real feedback (M23 live proof:
  accepted → +0.41 after repetition decay, dismissed → −1.0), and the
  precision auto-downgrade writes the same `ambient_policies` ledger the
  bandit learner will write.
- The harness configs give M25 its measuring stick: the spec's gate is
  "learning-on ≥ static policy on intervention precision; zero
  learner-caused tier-0 escalations", evaluated on this battery plus the
  delivery-policy scoring.

## 6. M25 — learning vs static policy (`result_learning_eval.json`)

Deterministic 30-round feedback simulation over the real substrate: a tier-1
category dismissed 80% of the time ("ops-alerts") and a tier-2 category
accepted 80% of the time ("digest-reports").

| condition | interventions (tier ≤ 1) | intervention precision | learner tier-0 escalations | final tiers |
|---|---|---|---|---|
| static (`off` — M23 rule only) | 5 | 0.20 | 0 | ops-alerts → 3, digest-reports → 2 |
| learning (`auto`) | 30 | **0.70** | **0** | ops-alerts → 3, digest-reports → **1** |

**Gate: passed.** Learning-on beats the static policy on intervention
precision (0.70 ≥ 0.20) with zero learner-caused tier-0 escalations. The
asymmetry is structural: the static rule can only demote noise (it did,
after 5 judged items); the learner demotes it earlier (3) AND promotes the
consistently valuable category into the notify tier — every promotion
clamped one step at a time with a hard tier-1 floor.

Live §14c-28 (real stack, tick-driven learner, qwen3.8-max as the stack's
model): in `auto`, three dismissals in a category produced the clamped
re-tier 1→2 with NO approval step (ledger row `source=learner`, reason
"mean reward −1.00 over 3 judged items"), and the one-click revert cleared
it (`reverted by user`). In `propose`, the same three-dismissal signal
produced a queued `learner_proposal` that left the live tier untouched
until `POST /ambient/policies/{id}/approve` applied it — the proposal
visible in the Ambient Ledger tab with its Approve control
(`docs/acceptance/ambient_m25/`). Both modes are fully implemented; auto is
not gated behind propose.
