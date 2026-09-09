# Documentation

Complete documentation for the Concierge Agent POC. `spec.md` at the repo root remains the **specification** (what the system must do); these documents describe **what was built and how to work with it**. All diagrams are Mermaid — they render directly on GitHub and diff in pull requests.

## Architecture

| Document | Contents |
|---|---|
| [architecture/overview.md](./architecture/overview.md) | The system in 10 minutes; C4 context, container, and component diagrams; deployment topology; end-to-end data flow; index of key design decisions |
| [architecture/data-model.md](./architecture/data-model.md) | Full ERD (all ten application tables), per-table semantics, static-vs-dynamic rules, checkpoint storage, schema-change workflow |
| [architecture/components.md](./architecture/components.md) | Class diagrams of the load-bearing internals: provider port and adapters, registry cache, middleware stack, worker factory, retrieval pipeline |
| [architecture/runtime-flows.md](./architecture/runtime-flows.md) | Sequence diagrams: graph-mode run, agentic-mode run, HITL pause/resume, MCP server plug-in, cache invalidation and cross-replica sync |
| [architecture/state-machines.md](./architecture/state-machines.md) | Run status lifecycle, MCP server states, the frontend HITL gate lifecycle |
| [architecture/resolution-ladder.md](./architecture/resolution-ladder.md) | Flowchart and rung-by-rung walkthrough of capability resolution, including the retrieval gate and full-catalog fallback |

## Decisions

| Document | Contents |
|---|---|
| [adr/](./adr/README.md) | Ten Architecture Decision Records: no broker (0001), provider port (0002), middleware precedence (0003), cache bypass default (0004), hybrid retrieval (0005), JSONB-before-pgvector (0006), OpenAI Responses API routing (0007), LISTEN/NOTIFY sync (0008), skills as markdown (0009), two orchestrator modes (0010) |
| [../CHANGELOG.md](../CHANGELOG.md) | Project history, milestone by milestone (M1–M8 plus post-M8 fixes) |

## API & interfaces

| Document | Contents |
|---|---|
| [api/rest-api.md](./api/rest-api.md) | Conventions (error envelope, status-code semantics) and the complete endpoint reference, router by router |
| [api/sse-events.md](./api/sse-events.md) | The chat/run event-stream contract: every event type, payload schemas, replay and ordering guarantees, client guidance |
| [api/workflow-dsl.md](./api/workflow-dsl.md) | Sub-agent workflow JSON reference: node types, edge rules, HITL form-gate questions, validation catalog, worked example |
| [api/skill-format.md](./api/skill-format.md) | The `.skill.md` authoring reference: frontmatter schema, binding rules, annotated example |

## Operations

| Document | Contents |
|---|---|
| [operations/runbook.md](./operations/runbook.md) | Day-2 procedures: lifecycle scripts, health checks, mode/model/cache flips, MCP recovery, HITL triage, incident quick reference |
| [operations/configuration.md](./operations/configuration.md) | Every environment variable and every runtime Settings key, with defaults, effects, and consumers |
| [operations/scaling.md](./operations/scaling.md) | The scale-out path: multi-replica realities (SSE stickiness, LISTEN/NOTIFY), memory→redis promotion, JSONB→pgvector swap |
| [operations/troubleshooting.md](./operations/troubleshooting.md) | Symptom-indexed fixes: provider errors, MCP issues, cache staleness, SSE drops, 409 semantics, and more |
| [operations/data-lifecycle.md](./operations/data-lifecycle.md) | Volumes, stop-vs-decom, seeds, run-data growth and purge, checkpoints, backup guidance |

## Development

| Document | Contents |
|---|---|
| [development/contributing.md](./development/contributing.md) | The spec-driven workflow, conventional commits, hard constraints reviews enforce, PR evidence expectations |
| [development/local-development.md](./development/local-development.md) | Both dev loops: full containerized stack and the fast uv/vite loop; keyless fake-LLM mode |
| [development/testing.md](./development/testing.md) | Test suite map, the fake-provider philosophy, dual-cache-mode gate, how-to-add checklists |
| [development/code-tour.md](./development/code-tour.md) | Guided tour of every backend and frontend module, plus a "where would I change X?" table |
| [development/prompts.md](./development/prompts.md) | Catalog of all nine prompt files: loaders, timing, template slots |

## Cross-cutting

| Document | Contents |
|---|---|
| [security.md](./security.md) | Honest POC security posture: non-goals, secrets handling, trust boundaries, prompt-injection exposure, hardening checklist |
| [observability.md](./observability.md) | Traces, the shared label set, structured logging, Prometheus metrics, OTel and LangSmith switches, token usage |
| [user-guide.md](./user-guide.md) | Operator walkthrough of all seven UI pages, task by task |
| [glossary.md](./glossary.md) | The decoder ring: 45 project terms, each grep-verified against the codebase |

## Evidence

| Document | Contents |
|---|---|
| [acceptance/](./acceptance/README.md) | The manual-UI acceptance record: stages 00–22, 130+ screenshots on real models, both orchestrator modes, all themes, cross-provider campaigns |
