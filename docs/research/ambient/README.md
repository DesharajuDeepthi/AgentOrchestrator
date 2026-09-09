# Ambient mode research suite

**Branch:** `ambient_mode_exp` · **Date:** 2026-08-25 · Successor campaign to
`docs/research/memory/` (the memory layers this design builds on merged in
PR #14).

Ambient mode = the concierge doing useful, governed work **while the user is
not chatting**: watching for events, holding standing intents ("tell me
when…"), running routines, anticipating during idle time, and deciding when —
and whether — to initiate contact.

| Doc | What it holds | Read it for |
|---|---|---|
| [01-current-state.md](01-current-state.md) | Inventory of the platform against the trigger→decide→execute→deliver→govern pipeline: what memory/M13–M18 already provides (scheduler, headless runs, HITL, ledger) and the honest gap list (no trigger substrate, idle detector specced-not-built, no standing-intent store, no delivery policy) | why this is a promotion of existing assets, again |
| [02-landscape-production.md](02-landscape-production.md) | ChatGPT Tasks/monitoring/Pulse lifecycle, Claude Code Routines (the most complete shipped design), Claude Tag, Letta sleep-time, LangChain ambient agents + Agent Inbox, Gemini/Copilot caps, Home Assistant, Postgres-only eventing verdicts — 12 transferable mechanisms + anti-patterns | which shipped ideas transfer, with limits and pricing |
| [03-landscape-research.md](03-landscape-research.md) | Mixed-initiative foundations, proactive-agent benchmark ceilings, interruption science, prospective-memory failure data, standing-query heritage, autonomy/safety findings, evaluation recipes — 15 evidence-backed design rules | the numbers behind every design call |
| [04-architecture-proposal.md](04-architecture-proposal.md) | **The design**: five planes (A1 triggers, A2 three-tier wake gate, A3 routines/intents/anticipation, A4 digest-first delivery, A5 governance), schemas, §8.9 UI, measured goals | the shape of the build |
| [05-requirements.md](05-requirements.md) | **Full requirements**: the framing decisions (mode-not-agent-type, work beyond consolidation, three heartbeat senses, autonomy ceilings), the closed 11-type trigger taxonomy FR-T1..T11, FR catalog per plane, NFRs with non-regression as the headline | what "feature-rich and non-regressive" means, testably |
| [05b-gap-research.md](05b-gap-research.md) | Gap research: agent-scheduled wakeups (PM-Bench heartbeat evidence, MemGPT request_heartbeat, ScheduleWakeup failure modes), adaptive polling, lease/heartbeat/reaper numbers from production job systems, CEP-lite (absence = armed timer) + four chaining guards, presence detection + delivery-tier heuristics | the concrete intervals, thresholds, and schemas |
| [06-spec-amendment-and-milestones.md](06-spec-amendment-and-milestones.md) | **Draft spec §17** (17.0–17.6 full text), §3.7 settings, §8.9 UI, §14c acceptance steps 20–27, milestones **M20–M24**, settled decisions + 4 open questions for sign-off | what merges into spec.md, and in what order |

## The design in one paragraph

Events (schedules, webhooks, pollers, internal signals) land as untrusted
rows in an append-only table and wake the existing advisory-locked scheduler
via NOTIFY pings; a three-tier gate — typed matchers, then one cheap
significance judgment, then a full run — decides fire vs hold, and every
decision is audited. Fires create ordinary runs with trigger provenance,
executing routines (trusted stored prompts over a narrowed registry
projection, hard budgets, an abstain instruction, autonomy ceilings that
route consequential actions through the existing HITL gates with
absent-user timeouts). Results flow through a delivery outbox governed by a
predictable digest with an urgency bypass, a daily notification budget,
quiet hours, and per-category feedback that tunes itself. Standing intents
are typed Postgres rows — never remembered prompts — compiled once from
natural language and evaluated by the scheduler. All of it is dark by
default behind `ambient_enabled=false`, byte-identical when off, and
measured on a simulated-clock event harness before anything ships on.
