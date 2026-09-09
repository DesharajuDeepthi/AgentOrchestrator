# ADR-0003: Middleware precedence — out-of-box first, custom last

Status: Accepted

Date: 2026-08-05

## Context

Every `create_agent` loop in the system — skill nodes, rung-1 inline
execution, ephemeral workers, the fallback loop, the agentic orchestrator —
needs the same cross-cutting behavior: context compaction, tool-iteration
limits, and live visibility of the Postgres registries. LangChain ships
middleware for the first two. The temptation in agent codebases is to write
custom middleware for everything, which duplicates upstream behavior, drifts
from it, and multiplies the surface that must be maintained when the
framework changes.

## Decision

A strict precedence order for middleware (spec §7.0, CLAUDE.md hard
constraint):

1. **Out-of-box LangChain middleware first**, configured via options:
   `SummarizationMiddleware` for compaction, `ModelCallLimitMiddleware` for
   `max_tool_iterations` (never custom counting), `TodoListMiddleware` for
   agentic planning.
2. **Compose or subclass an existing hook second**, when configuration alone
   does not suffice.
3. **Custom middleware only when nothing out-of-box fits.** Exactly three
   customs are sanctioned — the registry projections in
   `backend/app/orchestrator/middleware.py`: `ToolsRegistryMiddleware`,
   `SkillsRegistryMiddleware`, `SubAgentsRegistryMiddleware`. They qualify
   because nothing off the shelf projects a Postgres registry into a model
   call. They are stateless projections (fresh read per call, reuse by
   class + config, never shared state).

All stacks are built through one helper, `build_middleware_stack(context)`:

- `SkillLoopContext` → Summarization + call limit + ToolsRegistry in
  **scoped mode** (bound tool ids only). SkillsRegistry/SubAgentsRegistry
  are never attached to a skill loop, enforcing the §3.3 isolation invariant
  structurally: a skill cannot see other skills, agents, or unbound tools.
- `FallbackLoopContext` → Summarization + limits + Tools/Skills registries
  in full-catalog mode.
- `AgenticLoopContext` → TodoList + Summarization + limits + all three
  registry middlewares, exposure-gated.

## Consequences

Positive:

- Registry freshness is uniform: a reconnected MCP server or newly saved
  skill is visible at the next model call in every loop, with no rebuild
  and no bespoke sync code per loop type.
- Security-relevant isolation (skill = bound tools only) is a property of
  stack construction, not of per-callsite discipline.
- Framework upgrades land in one place; the OOB middlewares track upstream
  fixes for free.

Negative:

- Coupling to LangChain's middleware API: a breaking change upstream hits
  every loop at once (mitigated by the single construction helper).
- The precedence rule adds review friction — a fourth custom middleware
  requires arguing that nothing OOB fits, by design.
- Fresh registry reads per model call put load on the read path; that cost
  is what ADR-0004 (registry cache) exists to absorb.

## References

- spec.md §7.0 (middleware layer), §3.3 (binding = availability)
- /home/user/concierge-agent/backend/app/orchestrator/middleware.py
  (`build_middleware_stack`, the three projections)
- Consumers: backend/app/orchestrator/{graph_mode,agentic_mode,ladder}.py,
  backend/app/factory/worker.py
- Related: ADR-0004 (the cache the projections read through), ADR-0010
  (the two orchestrator modes sharing these stacks)
