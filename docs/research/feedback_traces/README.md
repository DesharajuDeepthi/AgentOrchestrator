# Feedback-trace completeness — research & architecture proposal (M44)

**Status:** proposal — decisions made, pending spec merge (§16.1 amendment).
**Lineage:** M43b separated the salience judge's reward from delivery
feedback; the M43c closure audit then asked *which approval/rejection
signals feed loops, and are all consumers gated?* and produced the §17.7
feedback-consumer rule (capture always-on; every consumer gated; nothing
ships hot). This document answers the question that audit left open:
**which signals leave no trace at all — and which of those should?**

---

## 1. The complete signal inventory

Every place a human accepts, rejects, corrects, or abandons something the
system did. "Durable" means it survives a restart and is attributable later.

| # | Signal | Captured? | Durable? | Consumer today | Gate |
|---|---|---|---|---|---|
| 1 | Inbox ✓/✕/· on a delivery | `Delivery.feedback` + blended reward | yes | §17.7 learner / §17.3 rule | `ambient_learning_mode` / `ambient_precision_rule_enabled` (M43c) |
| 2 | Salience Do it / Leave it / Undo | `judge_reward` ±1 on the salience record (M43b) | yes | none (by design; future learner enters gated) | n/a |
| 3 | Opening a delivery | `seen_at` (M42) | yes | unread badge, salience prefilter | n/a (fact, not loop) |
| 4 | HITL approve/deny (chat, ambient, A2A) | run steps / checkpoints | yes | none — **consent gate, not preference signal** (§17.7 rule) | forbidden as side effect |
| 5 | §17.7 policy proposal — approve | policy row activates | yes | applies that change | n/a |
| 6 | §17.7 policy proposal — **reject** | **NOT CAPTURABLE** — no endpoint; pending proposals sit forever | — | — | — |
| 7 | §17.7 applied policy — revert | clearing ledger row | yes | none (audit) | n/a |
| 8 | Memory edit | edit-as-supersede chain (§16.1) — the correction delta persists | yes | none yet | n/a |
| 9 | Memory quarantine review approve/reject | row keeps `status='rejected'` + `review_note` | yes | none yet | n/a |
| 10 | Memory **user deletion** | **NOTHING** — `hard_delete` is physical | no | — | — |
| 11 | Overlap-guard override (save despite warning) | **NOTHING** | no | — | — |
| 12 | Stop button / eval grade disagreement / toast interaction | run status / n/a / n/a | partial | none | n/a |
| 13 | Implicit chat signals (regenerate, rephrase-after-answer) | not captured | no | — | — |

Rows 1–5 and 7–9 are healthy: captured, durable, consumers gated. The gaps
are rows **6, 10, 11** — and row 13 is inventoried to be *deliberately
excluded* (below).

## 2. The deletion gap is a correctness bug, not just a missing signal

`hard_delete` (`app/memory/store.py:326`) physically removes the row.
Admission (`reconcile_and_write`, `app/memory/extract.py:117`) reconciles a
candidate **only against memories that still exist**. Consequence: delete a
memory, mention the same fact in any later conversation, and extraction
**re-admits what the user explicitly removed**. Deletion is not durable —
the delete button quietly promises less than it appears to.

The root cause is that one button collapses two different intents:

- **"Forget this"** — *wrong, noisy, or unwanted; don't keep it and don't
  re-learn it.* Durability here **requires** a trace: something for the
  admission gate to check candidates against. That same trace is exactly
  the learning signal the M43c audit wanted ("what kinds of memories do
  users reject?").
- **"Erase this"** — *leave nothing, including the fact that something was
  removed.* The current physical delete is the correct implementation of
  this intent and must survive as an explicit verb. Privacy wins here **by
  the user's explicit choice**, never by accident.

## 3. Design: two verbs + tombstones

### 3.1 The tombstone privacy taxonomy

What could a "forget" trace retain? Four tiers, in increasing utility and
increasing privacy weight:

| Tier | Contents | Enables | Privacy cost |
|---|---|---|---|
| 0 | counters per kind/scope | trend metrics only | none |
| 1 | metadata row: kind, scope, source, confidence-at-admission, importance, age-at-forget, access_count, pinned?, forgotten_at | learning ("reflections in scope X get forgotten 80% of the time") | leaks that *something* of a category existed |
| 2 | + SHA-256 of normalized text | exact re-admission suppression | hash of short/guessable text is dictionary-attackable — honest caveat, not a secret-keeper |
| 3 | + embedding vector (copied from `memory_embeddings`) | **semantic** suppression — catches paraphrases | quasi-content: embeddings partially invert; strongest trace |

**Decision: Tier 2 always + Tier 3 opportunistically.** A tombstone stores
the metadata and the normalized-text hash; when an embedding exists for the
row at forget time, it is copied onto the tombstone and used *only* for
suppression matching. No embedding configured ⇒ the system degrades to
exact-hash suppression automatically — no extra knob for that. The
embedding copy lives exactly as long as the tombstone: **unforget, erase,
and §8.7 purge all destroy it**.

### 3.2 Verbs and their guarantees

| Verb | Trace left | Re-admission | Reversible |
|---|---|---|---|
| **Forget** (UI primary when enabled) | tombstone (Tier 2/3) | suppressed | yes — *unforget* deletes the tombstone; the fact becomes learnable again (the memory itself is gone either way) |
| **Erase** (explicit) | none — today's `hard_delete` | possible (inherent: nothing exists to check against; the UI says so) | no |
| **Purge** (§8.7) | none — clears memories **and all tombstones** | n/a | no |

Quarantine review-reject (row 9) keeps its current, stronger behavior: the
row itself persists as `status='rejected'` — that path was never physical.

### 3.3 Suppression semantics (the §16.2 hook)

In `reconcile_and_write`, before any write:

1. candidate normalized-text SHA-256 equals a live tombstone's hash → **suppressed**;
2. else, the **hybrid gate**: cosine ≥ `memory_forget_similarity`
   (default **0.85**) suppresses alone; in the gray band (≥ 0.70) a shared
   **distinctive-token anchor** is also required — the tombstone keeps
   payload-token hashes (Tier-2 privacy, same dictionary caveat). The
   live campaign measured real paraphrase pairs at **0.876** and
   **0.847**: a lone threshold cannot separate restatements from
   value-updates, but true restatements carry the fact's payload token
   and updates carry a new one — the anchor separates what cosine
   cannot. Calibration by evidence, not intuition.

Matching is tenant-scoped (`user_id` under auth) and scope-aware, mirroring
recall. A suppressed admission is not silent: it logs
`memory_admission_suppressed` (§10 labels; no content) and increments the
tombstone's `suppressed_count` / `last_suppressed_at` — which is the
accruing evidence ("this forgotten fact keeps trying to come back") a
future learner or the Forgotten UI can read. Suppression applies to the
`extracted`/`inferred`/salience-retain machine paths. A **user-stated**
write (`memory.remember`, manual create) **overrides**: the human
re-asserting a fact beats their earlier forget — it deletes the matching
tombstone and admits (ledgered as an unforget-by-assertion).

### 3.4 Nothing hidden: the Forgotten section

Suppression must never look like "memory mysteriously won't learn X." The
Memory page gains a **Forgotten** section listing tombstones — kind, scope,
source, when, `suppressed_count` (never text; there is no text) — each with
an **Unforget** control. That is the escape hatch, and it completes the
same honesty contract the M43 card follows: consequence first, mechanism
visible, reversal where reversal is physically possible.

### 3.5 Gating and byte-identity

Changing what the delete button *means* is an observable behavior change,
so it enters dark (house discipline, and §17.7's "no consumer ships hot"):

- `memory_forget_enabled` (default **false** — byte-identical: deletion
  stays physical, no tombstones, no suppression, `mode=forget` is a 422
  naming the setting).
- `memory_forget_similarity` (default 0.85, range 0.5–1.0) — the semantic
  suppression threshold; hint states hash-only fallback when no embedding
  model is configured.

Both on the Settings Memory block per the §8.7 completeness rule. The
Settings hint for the master states the trade plainly: *"off = deletes are
physical and the system may re-learn a deleted fact."*

## 4. The two small captures riding the same wave

- **§17.7 proposal reject** (row 6): `POST /ambient/policies/{id}/reject`
  — the pending proposal's row moves to `status='rejected'` with a
  timestamp, capture-only (no consumer; a rejected proposal is itself
  feedback about the learner, and today that signal is expressed by
  letting rows rot). UI: a reject control beside approve.
- **Overlap-guard override** (row 11): v1 captures a content-free
  `overlap_override` structlog event + §10 counter (draft kind, overlap %,
  threshold in force). Deliberately **not** a table yet: no consumer is
  designed, authoring telemetry is lower-stakes than user feedback, and
  promoting it to durable storage is a two-line change the day a
  threshold-tuner is spec'd. This proportionality note is on the record so
  the choice is a decision, not an omission.

## 5. Deliberately excluded — and why, on the record

- **HITL/A2A approvals as training data**: consent gates, not preference
  signals (§17.7 rule, verbatim). "The user always approves, stop asking"
  is automation creep on a safety mechanism.
- **Implicit chat signals** (regenerate = dissatisfaction, rephrase =
  failure, dwell time): the richest and creepiest tier. Capturing them
  means instrumenting the user rather than the system, the signals are
  noisy proxies, and none of the POC's learners could consume them
  honestly. Inventoried so the exclusion is visible; excluded.
- **Memory-edit deltas**: already captured (edit-as-supersede, §16.1) —
  listed because the audit initially mis-flagged it.

## 6. Milestone plan

**M44 — durable forgetting + trace completeness (§16.1/§16.2/§17.7/§8):**
migration (`memory_tombstones` + partial indexes on hash and user/scope),
`forget`/`erase`/`unforget` in the store, the admission suppression hook,
user-assertion override, API (`DELETE /memories/{id}?mode=`, tombstone
list/unforget endpoints, policy reject), Memory-page Forgotten section +
verbs, Settings keys, ~14 contract tests (forget→tombstone, exact + semantic
suppression, hash-fallback, unforget restores learnability, user-stated
override, erase leaves nothing, flag-off byte-identity, tenancy, purge
clears tombstones, suppressed_count accrual, proposal reject), stage-32
live evidence. Byte-identical at defaults.

**Explicit non-goals:** any consumer of tombstone data (enters later under
its own `off|propose|auto` gate per §17.7); tombstone TTL (purge is the
reset; a TTL is future polish); durable overlap-override storage.
