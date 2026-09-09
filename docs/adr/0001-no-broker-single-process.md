# ADR-0001: Single asyncio FastAPI process, no message broker

Status: Accepted

Date: 2026-08-04

## Context

The Concierge Agent POC must prove a registry-driven, tri-layer agent
architecture (orchestrator → sub agents → skills → tools) end to end. Agent
runs are long-lived, streamed over SSE, can pause for human-in-the-loop
approval, and dispatch sub agents in parallel. The reflexive architecture for
this shape of workload is a broker: Celery or a task queue for run execution,
Redis for pub/sub and state, workers separate from the API process.

The POC's success criterion, however, is architectural (do the registries and
the resolution ladder work?), not operational (does it scale?). Every extra
stateful service raises the cost of `docker compose up`, of testing, and of
reasoning about failure modes — for no evidentiary gain at POC scale.

## Decision

Runs execute as plain asyncio tasks inside the single FastAPI process
(spec §2). There is no message broker, no task queue, no Celery, and no
required Redis. The compose stack is exactly three services: `db`, `backend`,
`frontend`. Concretely:

- SSE streaming is plain HTTP from the same process that runs the graph.
- HITL pause/resume rides the LangGraph Postgres checkpointer — an interrupt
  persists the run state; `POST /runs/{id}/hitl` resumes from checkpoint.
- The registries are **passive shared state in Postgres**: two write paths
  (static seed, admin UI) and one read path (the orchestrator). Nothing
  "publishes" registry changes to executors; executors read fresh state on
  the next model call (see ADR-0003 and ADR-0004).
- Execution across tiers is in-process function/graph calls — dispatching a
  sub agent invokes a compiled subgraph on the shared checkpointer, not a
  queued job.
- Postgres is the only required stateful infrastructure. Redis exists solely
  as an *optional* registry-cache backend behind a compose profile
  (ADR-0004); no other subsystem may depend on it.

## Consequences

Positive:

- One-command startup and a trivially reproducible acceptance environment;
  the ten-step demo script (spec §14) runs on a fresh `docker compose up`.
- No serialization boundary between orchestrator and workers: interrupts,
  exceptions, and `usage_metadata` propagate as Python objects.
- Cooperative cancellation is a task cancel at the next step boundary, not a
  distributed revoke.
- Tests exercise the real execution path without broker fakes.

Negative:

- Throughput is bounded by one process; a crash kills all in-flight runs
  (checkpoints survive, in-flight LLM calls do not).
- Horizontal scaling requires revisiting run placement — the checkpointer
  makes resume portable, but live SSE streams are pinned to the process that
  owns the task.
- Long CPU-bound work in a run would block the event loop; all heavy work
  must stay async (LLM and MCP I/O are).

## References

- spec.md §2 (Stack), §7.1 (cancellation), §7.2 (HITL propagation)
- /home/user/concierge-agent/docker-compose.yml
- /home/user/concierge-agent/backend/app/orchestrator/runner.py
- Related: ADR-0004 (registry cache), ADR-0008 (LISTEN/NOTIFY, the dormant
  multi-replica path)
