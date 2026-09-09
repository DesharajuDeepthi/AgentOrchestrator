# Feedback-loop experiment report (FLE-4)

**Branch:** `feedback_loop_exp` · **Harness:** `experiments/feedback_loop/`
(deterministic, key-free, replaying the SHIPPED pipeline — real
`add_delivery`, real flush, real `run_salience_pass`, real `decide()`).
**Bar (set before the learner existed):** precision ≥ the static
baseline, missed-critical no worse, zero clamp violations.

## 1. Salience tuner vs the static frontier

World: `prod-incidents` (urgency 4–5, endorsement 0.9), `build-noise`
(3–4, 0.05), `batch-info` (1–2, 0.5); 30 rounds; scripted eager judge;
scripted human deciding through the shipped decision engine.

| condition | judge calls | precision | missed-critical | judge reward | clamp violations |
|---|---|---|---|---|---|
| static floor 2 | 75 | 0.493 | 0 | −1 | — |
| static floor 3 (shipped default) | 60 | 0.483 | 0 | −2 | — |
| static floor 4 (best static) | 45 | 0.622 | 0 | +11 | — |
| static floor 5 | 15 | 0.867 | **15** | +11 | — |
| **learner (auto, start 3)** | 55 | **0.655** | **0** | **+17** | **0** |

**Verdict: the bar is cleared.** 0.655 > 0.622 (best static) and > 0.483
(the default it started from), at zero missed-critical, zero clamp
violations. Total judge reward (+17) beats every static point.

**Dynamics, reported rather than tuned away:** the tuner muted
`build-noise` once its window filled (endorsement 0.067 ≤ 0.10), which
left a golden prod-only mix (0.9 > 0.8) — so the floor-down rule slid the
floor to 2, admitting `batch-info` (0.533). That trade lowered precision
from a theoretical mute-only ~0.9 to 0.655 while raising total reward.
A more conservative `FLOOR_DOWN_ABOVE` would hold higher precision at
lower reward; we deliberately did NOT tune this against the harness
world — over-fitting the synthetic world is the anti-pattern the M24
precedent warns about. The floor track was `3→3→2→2→2→2→2`: one move,
then stable — no oscillation.

**Rule change forced by the harness** (also noted in the tuner's
docstring): the research doc's mute trigger said "zero endorsements";
a noise category that is still 5% valuable never hits zero in a rolling
window, so the shipped rule is endorsement ≤ 0.10 over ≥ 5 decided.

**Harness bug found and fixed honestly:** the first baseline run judged
only 12 of 90 candidates — the M23 notification budget (3/day, by
design) was throttling tier-0 flushes and degenerating the endorsement
patterns. The harness world now runs the delivery plane unthrottled,
with the reason commented in the script.

## 2. Forget-gate sweep (M44 hybrid gate, quantified)

Populations seeded at the two live-measured paraphrase cosines
(0.876, 0.847), 40 each of paraphrases / value-updates / unrelated:

| configuration | false-admit (paraphrases) | false-suppress (others) |
|---|---|---|
| threshold-only 0.85 | 0.200 | 0.025 |
| threshold-only 0.80 | 0.000 | 0.175 |
| **shipped hybrid (0.85 / band 0.70 / anchors)** | **0.025** | **0.050** |

The anchor mechanism is decisively on-frontier — every frontier point
uses anchors. The shipped 0.85 sits one grid step off this synthetic
frontier (0.86–0.90 dominate it through exactly two synthetic update
points near 0.85). **This is population-sensitive, not grounds to
re-ship a default** — it is precisely the knob a future forget-tuner
should adjust from real per-install suppression history, which is why
`memory_forget_similarity` is a live setting.

## 3. Extraction tuner vs the static frontier (M47, closure_pack)

The second consumer, built after the first proved the pattern (the
scope decision the original §3 of this report recorded). Harness:
`extraction_eval.py` — real `gate_candidates` (live floor), real
`remember()` (live kind router), real `tombstone_forget`, real review
transitions, real tuner; two worlds, one per lever. Bar set in the
harness docstring before the learner existed.

**World A — kind-concentrated junk** (`entity` 90%-forgotten at
confidence 0.55–0.75, overlapping `preference` from 0.72; no static
floor can separate them):

| condition | junk admitted | valuable blocked | tombstones (human forget workload) | clamps |
|---|---|---|---|---|
| static 0.5 (shipped default) | 60 | 0 | 75 | — |
| static 0.6 | 42 | 0 | 58 | — |
| static 0.7 | 15 | **24** | 28 | — |
| static 0.75 | 5 | **42** | 16 | — |
| **learner (auto, start 0.5)** | **10** | **0** | **30** | **0** |

The learner routed `entity` after its first window and beat every
zero-loss static point (10 vs 42–60) with zero valuable loss; the
statics that admit less junk pay for it in blocked valuable writes.
**Honest cost:** routing converts junk admissions into review-queue
items — 45 rejections landed in the review queue. Cheaper per item
than active-memory pollution, but not free; the counter is reported.
The floor never moved in this world (the junk is not band-local) —
routing did all the work, which is the division of labor by design.

**World B — cross-kind low-confidence junk** (everything below
confidence 0.62 is junk regardless of kind; no kind crosses the
routing rate — only the floor can act):

| condition | junk admitted | valuable blocked | clamps |
|---|---|---|---|
| static 0.5 (shipped default) | 66 | 0 | — |
| static 0.6 (retrospective oracle) | 12 | 0 | — |
| static 0.65 | 0 | **18** | — |
| **learner (auto, start 0.5)** | **36** | **0** | **0** |

Floor track `0.5→0.5→0.55→0.60→0.60…`: the walk found the
zero-collateral point (0.60) and stopped — the band above is mostly
kept, so it never took the 0.65 step that blocks valuable writes.

**Bar item 1 FAILS in world B as literally stated**, and that is
reported, not reworded: the learner (36) does not beat the
retrospective oracle static (12). A learner whose only lever is the
same dial the static uses cannot beat that dial's oracle inside the
window — it can only converge to it, and the 24-admission gap is the
price of learning 0.62 instead of being told. What world B does prove:
the walk is correct, the stop is at the zero-collateral point, nothing
valuable was blocked, no clamp was violated, and against the shipped
default — the honest production comparison, since no one knows the
oracle in advance — it wins 36 vs 66. World A, where the learner has a
lever no static has, clears its bar outright. The ship decision rests
on that full picture, plus: the gate is born dark (`off`), `propose`
routes every change through the review queue, and both dials remain
plain Settings fields a human can override in one edit.

**Rule refinement forced by the harness** (recorded in the tuner
docstring, the §1 precedent): the research doc's floor trigger — "a
kind's forget-rate is persistently high" — ratchets the floor against
confidence-independent repudiation until it starves valuable kinds.
The shipped trigger is band-local: the floor rises only when the band
a +0.05 bump would newly refuse is itself ≥ 60% repudiated, which the
tombstones' confidence-at-admission metadata makes measurable. Known
limitation, stated: a band walk cannot jump a gap — junk that is not
contiguous with the floor is the router's job or a human's.

## 4. Exit criterion, applied

Per the FLE-1 bar, for M45: (1) precision ≥ baseline ✅ (2)
missed-critical no worse ✅ (3) zero clamp violations ✅ (4) this report
keeps the counter-evidence visible (the precision-vs-reward trade in
§1, the threshold sensitivity in §2). The branch was merged to `dev` as
milestone M45 (PR #20).

For M47 (§3): world A clears the bar outright; world B fails bar item 1
against the retrospective oracle and that failure is analyzed above at
full prominence — the ship rationale is convergence-to-oracle at zero
collateral plus the world-A win, with the gate dark by default.
