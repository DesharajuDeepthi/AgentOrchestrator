"""Run/step recording (spec §3.6) + trace emission (spec §10).

Every step start/finish writes a run_steps row, a structlog event with the
shared label set, Prometheus metrics, an OTel span, and the matching SSE
events. Token totals roll up onto the run.
"""

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from app import obs
from app.db import get_session_factory
from app.models import Run, RunStep
from app.orchestrator.context import EVENT_BUS

logger = structlog.get_logger("run")


def sse_event(event_type: str, run_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event_type,
        "run_id": str(run_id),
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload,
    }


class RunRecorder:
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        self._starts: dict[UUID, float] = {}
        self._labels: dict[UUID, dict[str, Any]] = {}
        self._tracer = obs.get_tracer()
        self._spans: dict[UUID, Any] = {}

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        EVENT_BUS.emit(self.run_id, sse_event(event_type, self.run_id, payload))

    async def start_step(
        self,
        step_type: str,
        *,
        tier: str,
        kind: str | None = None,
        source: str | None = None,
        entity_id: str | None = None,
        entity_name: str | None = None,
        node_id: str | None = None,
        sub_agent_id: str | None = None,
        parent_step_id: UUID | None = None,
        model: str | None = None,
        effort: str | None = None,
        input: dict[str, Any] | None = None,
        emit_dispatch: bool = False,
    ) -> UUID:
        async with get_session_factory()() as session:
            step = RunStep(
                run_id=self.run_id,
                parent_step_id=parent_step_id,
                sub_agent_id=UUID(sub_agent_id) if sub_agent_id else None,
                node_id=node_id,
                step_type=step_type,
                input=input,
                model=model,
                status="running",
            )
            session.add(step)
            await session.commit()
            step_id = step.id
        self._starts[step_id] = time.monotonic()
        if entity_id:
            # retrieval pin (spec §7.4): entities used in this run stay
            # visible in ranked catalogs for the rest of the run
            from app.orchestrator.context import get_run_context

            ctx = get_run_context()
            if ctx is not None:
                ctx.pinned_ids.add(str(entity_id))
        labels = obs.label_set(
            run_id=str(self.run_id),
            step_id=str(step_id),
            tier=tier,
            kind=kind,
            source=source,
            entity_id=entity_id,
            entity_name=entity_name,
            model=model,
            effort=effort,
            status="running",
        )
        self._labels[step_id] = labels
        logger.info("step_start", step_type=step_type, **labels)
        span = self._tracer.start_span(f"{step_type}:{entity_name or node_id or tier}")
        for key, value in labels.items():
            if value is not None:
                span.set_attribute(f"concierge.{key}", str(value))
        self._spans[step_id] = span
        # live activity feed (spec §7.1): every step transition, so the chat
        # can show what the run is doing right now without exposing payloads
        self.emit(
            "activity",
            {
                "step_id": str(step_id),
                "parent_step_id": str(parent_step_id) if parent_step_id else None,
                "step_type": step_type,
                "tier": tier,
                "kind": kind,
                "entity_name": entity_name,
                "node_id": node_id,
                "status": "running",
            },
        )
        if emit_dispatch:
            self.emit(
                "dispatch_start",
                {
                    "step_id": str(step_id),
                    "tier": tier,
                    "kind": kind,
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                },
            )
        return step_id

    async def finish_step(
        self,
        step_id: UUID,
        *,
        status: str = "completed",
        output: dict[str, Any] | None = None,
        error: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        emit_dispatch: bool = False,
    ) -> None:
        duration_ms = int((time.monotonic() - self._starts.pop(step_id, time.monotonic())) * 1000)
        labels = self._labels.pop(step_id, {})
        labels.update(
            {
                "status": status,
                "duration_ms": duration_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )
        async with get_session_factory()() as session:
            step = await session.get(RunStep, step_id)
            if step is not None:
                step.status = status
                step.output = output
                step.error = error
                step.input_tokens = input_tokens
                step.output_tokens = output_tokens
                step.finished_at = datetime.now(UTC)
                await session.commit()
            run = await session.get(Run, self.run_id)
            if run is not None and (input_tokens or output_tokens):
                run.total_input_tokens += input_tokens
                run.total_output_tokens += output_tokens
                await session.commit()
        logger.info("step_finish", **labels)
        tier = str(labels.get("tier") or "orchestrator")
        kind = str(labels.get("kind") or "-")
        source = str(labels.get("source") or "-")
        obs.STEPS_TOTAL.labels(tier=tier, kind=kind, source=source, status=status).inc()
        obs.STEP_DURATION.labels(tier=tier, kind=kind, source=source).observe(duration_ms / 1000)
        if input_tokens:
            obs.STEP_TOKENS.labels(tier=tier, kind=kind, source=source, direction="input").observe(
                input_tokens
            )
        if output_tokens:
            obs.STEP_TOKENS.labels(tier=tier, kind=kind, source=source, direction="output").observe(
                output_tokens
            )
        if status == "failed":
            obs.ERRORS_TOTAL.labels(tier=tier, kind=kind, source=source).inc()
            self.emit("error", {"step_id": str(step_id), "message": error or "step failed"})
        span = self._spans.pop(step_id, None)
        if span is not None:
            span.set_attribute("concierge.status", status)
            span.set_attribute("concierge.duration_ms", duration_ms)
            span.end()
        self.emit("activity", {"step_id": str(step_id), "status": status})
        if emit_dispatch:
            self.emit(
                "dispatch_end",
                {
                    "step_id": str(step_id),
                    "tier": labels.get("tier"),
                    "kind": labels.get("kind"),
                    "entity_id": labels.get("entity_id"),
                    "entity_name": labels.get("entity_name"),
                    "status": status,
                },
            )

    async def record_route(
        self,
        *,
        capability: dict[str, Any],
        rung: str,
        resolved_to: dict[str, Any],
        kind: str | None = None,
        source: str | None = None,
    ) -> None:
        """The ladder is deterministic and logged as a `route` step (spec §7.1)."""
        step_id = await self.start_step(
            "route",
            tier="orchestrator",
            kind=kind,
            source=source,
            entity_id=resolved_to.get("entity_id"),
            entity_name=resolved_to.get("entity_name"),
            input={"capability": capability},
        )
        await self.finish_step(step_id, output={"rung": rung, "resolved_to": resolved_to})
        self.emit(
            "route",
            {"capability": capability, "rung": rung, "resolved_to": resolved_to},
        )
