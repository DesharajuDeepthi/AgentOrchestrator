# Feedback-loop learners — research & architecture (feedback_loop_exp)

**Status:** experiment branch. Per the §17.7 feedback-consumer rule and the
house precedent (M24 harness before the M25 learner counted), **nothing on
this branch reaches a PR until it beats the static baseline on a measured
harness.** The evidence bar is at the bottom and it is the exit criterion,
not an aspiration.

**Lineage:** M43b gave the salience judge its own reward ledger
(`judge_reward`); M43c wrote the rule that every consumer of feedback is
individually gated; M44 made forgetting durable and left tombstones
accruing metadata with an explicit "no consumer yet" note. This branch
builds the consumers — gated, clamped, evidence-first.

---

## 1. What exists to learn from (the accrued ledgers)

| Ledger | Since | Fields a learner may read | What it measures |
|---|---|---|---|
| Salience records on deliveries | M42/M43b | verdict, confidence, mode, decision (applied/declined/undone), `judge_reward` ±1, category, urgency | per-category judge accuracy as endorsed vs repudiated verdicts |
| Memory tombstones | M44 | kind, source, scope, confidence-at-admission, importance, age-at-forget, `suppressed_count` | what kinds of machine writes users reject, and how hard rejected facts try to come back |
| Quarantine review outcomes | M13+ | `status='rejected'` rows + review notes | extraction quality at the review gate |
| Proposal outcomes | M25/M44 | approved / **rejected** (`learner_rejected`, M44) / reverted | the meta-signal: how often the learner's own proposals are trusted |
| Delivery feedback | M23 | accepted/dismissed/ignored + blended reward | already consumed (M25 learner, §17.3 rule) — **not this branch's input**, per M43b separation |

## 2. The two learners

### 2.1 Salience tuner

Consumes the `judge_reward` ledger per category over a sliding window
(mirroring §17.3's shape: window 20, min sample 5). Proposes:

- **Urgency-floor moves** — `ambient_salience_min_urgency` ±1 step at a
  time, clamped to [2, 5], at most one move per evaluation window. Floor
  up when low-urgency judgments are chronically repudiated (wasted calls,
  wrong escalations); floor down only when endorsement is high AND the
  prefilter is demonstrably starving the judge.
- **Per-category mutes** — "stop judging category X": proposed when a
  category accrues ≥ 5 repudiations with zero endorsements. Stored as
  `AmbientPolicy` rows with `category='salience:<cat>'` — deliberately
  reusing the existing append-only policy ledger, so the **M25 revert
  endpoint, the M44 reject endpoint, and the proposal review UI all work
  on day one** with no new machinery.

Gate: `ambient_salience_learning` (`off`|`propose`|`auto`, default `off`).
Born dark. In `propose`, adjustments ride the existing proposal queue —
including the Reject control M44 added, whose captures are themselves this
learner's trust meta-signal.

### 2.2 Tombstone-informed extraction tuner

Consumes tombstone metadata + quarantine rejections per (kind, source).
Proposes:

- **Admission-floor moves** — the §16.2 gate's `GATE_MIN_CONFIDENCE`
  becomes a live setting (promoted under the M40 pattern: default equal to
  the constant it replaces); the learner proposes ±0.05 steps, clamped to
  [0.5, 0.9], when a kind's forget-rate from machine sources is
  persistently high.
- **Kind-quarantine proposals** — route a chronically forgotten kind's
  machine writes through the existing §16.2 review queue instead of
  admitting them directly.

Gate: `memory_extraction_learning` (`off`|`propose`|`auto`, default `off`).

### 2.3 Deliberate design decision: deterministic rules, not bandits (v1)

The M25 learner is a bandit over *delivery feedback* — a signal with real
volume. Salience decisions and forgets are **rare events**; a bandit over
five samples is noise wearing a costume. v1 learners are therefore
deterministic threshold rules with clamps and windows (the proven §17.3
shape). A bandit upgrade is justified only if the harness shows the
deterministic rules leaving measurable precision on the table at realistic
volumes. This is recorded as a decision, not an omission.

### 2.4 What stays out, restated

HITL/A2A approvals (consent, not preference — §17.7 verbatim); implicit
chat signals; delivery feedback (owned by the M25 learner — the M43b
separation is load-bearing and this branch must not re-couple it).

## 3. The harness (built BEFORE the learners)

`experiments/feedback_loop/` — M24's discipline: simulated clock, scripted
events, deterministic on the fake provider, key-free.

- **World generator**: category profiles (true criticality, noise rate),
  delivery streams (urgency, templated bodies), and a **scripted human**
  — per-category endorse/repudiate probabilities that constitute ground
  truth the learners never see directly.
- **Replay**: the real prefilter → a scripted judge → the real decision
  endpoints, so the code under test is the shipped pipeline, not a copy.
- **Metrics**: intervention precision (endorsed / surfaced escalations),
  missed-critical rate, judge-call cost, and — for the forget gate — a
  false-suppress / false-admit tradeoff sweep with the cosine
  distribution **seeded at the two live-measured points (0.876, 0.847)**
  so the M44 hybrid-gate choice gets a quantitative check, not just the
  two anecdotes that motivated it.
- **Baseline**: static floor 3, no mutes, learners off. Every learner run
  replays the identical stream.

## 4. The evidence bar (exit criterion)

A PR from this branch to `dev` requires, on the harness across gate
ablations:

1. learner intervention precision ≥ static baseline;
2. missed-critical rate no worse than baseline;
3. **zero clamp violations** across every run;
4. the experiment report (this directory) showing the numbers, the
   ablations, and any result that argues *against* shipping — kept with
   the same prominence as the ones that argue for it.

Anything short of that: the branch stays an experiment and the report
says so. The capture substrate on `dev` loses nothing by waiting — the
ledgers accrue either way.
