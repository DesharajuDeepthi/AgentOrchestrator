# M25 acceptance evidence — §14c-28

Live on the running stack (tick-driven learner):

- auto: 3 dismissals in `demo-noisy` → clamped re-tier 1→2 with NO approval, ledgered `source=learner`, one-click revert cleared it.
- propose: same signal in `demo-noisy2` → queued `learner_proposal` (tier untouched pre-approval) → applied only on `POST /ambient/policies/{id}/approve`.
- `36-ambient-learning-proposal.png`: the queued proposal with its Approve control in the Ambient Ledger tab.
