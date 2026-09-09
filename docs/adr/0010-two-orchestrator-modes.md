# ADR-0010: Two orchestrator modes, side by side and runtime-switchable

Status: Accepted

Date: 2026-08-05

## Context

There are two credible architectures for the top of the tri-layer stack. A
hand-built graph gives determinism: an explicit planner artifact, pure-code
capability resolution, auditable routing. A single tool-loop agent gives
adaptivity: emergent planning, mid-run self-correction, graceful handling of
surprises. Agent teams argue about this choice endlessly, usually on
intuition, because the two are rarely implemented over the *same* substrate
where they could be compared honestly. This POC is exactly that substrate.

## Decision

Build **both** and keep them (spec §7):

- **Graph mode** (default, `backend/app/orchestrator/graph_mode.py`): a
  hand-built LangGraph `StateGraph` — `plan → resolve → dispatch (parallel)
  → aggregate` — never `create_agent` at the shell. The planner is a
  structured-output call producing a validated plan artifact (schema check,
  id check, one repair retry, then clean failure). Resolution walks the
  **deterministic capability ladder** (direct tool/skill → native sub agent
  → custom sub agent → ephemeral worker), logged as `route` steps.
  Independent entries dispatch concurrently via `Send`.
- **Agentic mode** (`backend/app/orchestrator/agentic_mode.py`): one
  `create_agent` concierge with the full middleware stack — TodoList,
  Summarization, limits, and all three registry projections — plus
  `spin_worker` and `use_full_catalog` tools. Planning is emergent
  (todo-driven); the ladder becomes tool-construction policy inside the
  middlewares; parallelism is limited to parallel tool calls in one turn.
- **Runtime-switchable per run**: `app_settings.orchestrator_mode`
  (`'graph' | 'agentic'`, default `graph`) — a Settings toggle, no deploy.
- **Everything else is shared**: registries, ladder policy, worker factory,
  MCP manager, provider layer, checkpointer, HITL interrupt/resume, the SSE
  event contract, run/step recording, and trace labels. Traces from both
  modes are directly comparable **by construction** — the point is to A/B
  explicit-planner vs agentic orchestration on identical registries.

## Consequences

Positive:

- The comparison is real, not rhetorical: the acceptance routing matrix ran
  one neutral prompt across mode × thinking and both modes independently
  chose the same capabilities; stages 19–21 repeated both modes across
  providers.
- Shared subsystems mean a fix lands in both modes at once (e.g. the HITL
  idempotent-replay dispatch, the fallback loop).
- Different failure postures become a selectable property: graph mode fails
  fast on unresolvable plans; agentic mode self-corrects with traced
  escalations.

Negative:

- Two orchestration code paths to test and maintain; every new SSE event or
  step type must be wired and verified twice.
- The shared-contract discipline is load-bearing: any mode-specific label or
  event drift silently destroys trace comparability.
- Agentic-mode parallelism is structurally weaker, so "same registries" does
  not mean "same latency" — comparisons must control for it.

## References

- spec.md §7 (mode switch), §7.1 (graph mode), §7.2 (agentic mode)
- /home/user/concierge-agent/backend/app/orchestrator/graph_mode.py,
  agentic_mode.py, ladder.py, planner.py
- /home/user/concierge-agent/docs/acceptance/README.md ("Graph vs agentic —
  what the traces show"; the routing matrix)
- Related: ADR-0003 (shared middleware stacks), ADR-0002 (shared provider
  layer)
