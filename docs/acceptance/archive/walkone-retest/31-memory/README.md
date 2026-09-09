# Stage 31 — Memory layers through the UI (spec §16, §8.8, §14b steps 12–19)

Live semantic configuration (`memory_enabled` + extraction + procedural on,
`fake:scripted` embeddings), captured on the running stack with real model
calls. Facts are taught in one conversation and recalled in *new*
conversations throughout.

| frame | shows |
|---|---|
| 01-settings-memory-section | Settings → Memory (§16): master switch, L2 extraction, L3 procedural, L4 reflection toggles; injection/pinned budgets, recall top-k, score floor, half-life |
| 02-chat-teach-facts | conversation A states dog/editor/cluster facts |
| 03-memory-store | Memory page: stat cards; extracted facts/entities/preference active, the deploy-branch pair (release-2026 active / main superseded), an INSTRUCTION quarantined, Review queue (1) |
| 04-memory-detail-drawer | drawer: bi-temporal valid/recorded times, importance/confidence, provenance run link |
| 05-superseded-history | status filter `superseded` → the old "deploy branch is 'main'" fact retained as history |
| 06-review-queue | review tab listing the quarantined instruction |
| 07-review-drawer | quarantine gate: "REVIEW REQUIRED — does not apply until approved", entity key `email.signature`, Approve/Reject + note, Pin, Delete |
| 08-review-approved | after Approve: quarantined 0, review queue empty |
| 08-pinned | pin action from the drawer (pinned counter 1) |
| 09-cross-conversation-recall | new conversation: "dog's name + deploy branch" → Biscuit + release-2026, *"updated from the earlier main, per your correction"* |
| 10-run-trace-memory-event | run trace of that answer: direct plan answer citing memory ids, per-step tokens |
| 11-abstention | "cat's name?" → correct abstention citing the dog memory rather than transplanting it |

Reproduce: `scratchpad/camp46/stage31-memory.mjs` (+ `31b`, `31c` follow-ups).
