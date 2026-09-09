# A2A Outbound — Research Suite

**Branch:** `a2a_xperiment` · **Date:** 2026-08-27 · Successor to the
memory (`docs/research/memory/`) and ambient (`docs/research/ambient/`)
waves; spec §19, milestones M37–M39.

Giving the concierge the ability to call external agents over the A2A
protocol (Linux Foundation / a2aproject): register a remote agent by its
Agent Card URL, compose its declared skills into the existing
Tools → Skills → Sub Agents tiers, authenticate per the card's declared
security schemes, gate remote questions through HITL, and hand
long-running remote tasks to the ambient substrate.

| Doc | What it holds | Read it for |
|---|---|---|
| [01-current-state.md](./01-current-state.md) | Eight-subsystem reuse audit with file:line seams | what existing machinery A2A plugs into, and where |
| [02-a2a-protocol-and-sdk.md](./02-a2a-protocol-and-sdk.md) | Verified protocol + `a2a-sdk` 0.3.26 facts (introspected, not recalled) | the exact client/auth surfaces we integrate against, incl. the SDK's auth-placement coverage gap |
| [05-architecture-proposal.md](./05-architecture-proposal.md) | The signed-off design: components, proxy behavior, persistence, milestones | how every piece works and why |
| [06-spec-amendment-and-milestones.md](./06-spec-amendment-and-milestones.md) | Spec merge mechanics + decisions + M37–M39 rows | what changed in `spec.md` and the sign-off trail |

## The design in one paragraph

Remote A2A agents are a fourth external-capability registry — a peer of
MCP servers — whose Agent Card skills are ingested as `kind='a2a'` tools
and therefore compose like anything else; calls ride the official SDK
behind a lazy per-call proxy with card-driven auth (apiKey / basic /
bearer / oauth2 client_credentials, credentials write-only with `env:`
indirection), remote `input-required` pauses become ordinary HITL gates,
remote output is untrusted-fenced, over-budget tasks park into an
ambient-polled table that delivers results through the existing outbox,
and `a2a_enabled=false` (the default) is byte-identical to a build
without any of it.
