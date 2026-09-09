"""The standing-watch compiler core (spec §17.4, shared per §18.5).

One compile path serves both authoring surfaces: the `ambient.watch`
native tool (chat) and `POST /watches/compile` (the Ambient page). NL is
compiled ONCE into a typed rule, the interpretation is echoed back, and
the watch stays 'proposed' until the user confirms — never silently
active, on either surface.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

VALID_FILTER_OPS = {"equals", "contains", "starts_with", "one_of", "regex"}


class WatchFilter(BaseModel):
    field: str
    op: Literal["equals", "contains", "starts_with", "one_of", "regex"] = "equals"
    value: str = ""
    values: list[str] = Field(default_factory=list)


class WatchCompile(BaseModel):
    """The compiler's structured output: one typed rule + the echo line."""

    mode: Literal["events", "poll", "state"]
    filters: list[WatchFilter] = Field(default_factory=list)
    poll_source: str | None = None
    poll_config: dict[str, Any] = Field(default_factory=dict)
    probe: str | None = None
    probe_config: dict[str, Any] = Field(default_factory=dict)
    op: Literal[">=", "<=", "=="] = ">="
    value: float = 0.0
    semantic_predicate: str | None = None
    cadence_s: int = 300
    echo: str


async def compile_and_propose(text: str) -> dict[str, Any]:
    """Compile NL → typed rule, store it as a 'proposed' standing intent,
    and return the echo for confirmation. Rejections come back as
    {"status": "rejected", "error": ...} — never an exception."""
    from app.ambient.triggers import (
        poll_source_specs,
        registered_poll_sources,
        registered_state_probes,
        state_probe_specs,
    )
    from app.db import get_session_factory
    from app.llm import ModelParams, get_model
    from app.models import StandingIntent
    from app.prompts import load_prompt
    from app.registry_cache import get_cache

    cache = get_cache()
    ref = await cache.setting("memory_extraction_model") or await cache.setting("default_model")
    model = get_model(str(ref), ModelParams(effort="low"))

    def _registry_lines(specs: dict[str, str]) -> str:
        # §18.3: the compiler prompt lists each live entry WITH its config shape
        return (
            ", ".join(
                f"{name} (config: {shape})" if shape else name
                for name, shape in sorted(specs.items())
            )
            or "(none registered)"
        )

    prompt = load_prompt("ambient_watch_compile").format(
        text=text[:2000],
        poll_sources=_registry_lines(poll_source_specs()),
        state_probes=_registry_lines(state_probe_specs()),
    )
    try:
        out = await model.with_structured_output(WatchCompile).ainvoke(prompt)
        assert isinstance(out, WatchCompile)
    except Exception as exc:  # noqa: BLE001 — compile failure is a clean refusal
        return {"status": "rejected", "error": f"could not compile the watch: {exc}"}

    compiled: dict[str, Any]
    if out.mode == "state":
        if not out.probe or out.probe not in registered_state_probes():
            return {"status": "rejected", "error": f"unknown state probe: {out.probe!r}"}
        condition_type = "state"
        compiled = {
            "probe": out.probe,
            "config": out.probe_config,
            "op": out.op,
            "value": out.value,
        }
    elif out.mode == "poll":
        if not out.poll_source or out.poll_source not in registered_poll_sources():
            return {"status": "rejected", "error": f"unknown poll source: {out.poll_source!r}"}
        condition_type = "event"
        compiled = {
            "poll": {"source": out.poll_source, "config": out.poll_config},
            "filters": [f.model_dump() for f in out.filters],
        }
    else:
        if not out.filters:
            return {
                "status": "rejected",
                "error": "an events watch needs at least one filter — a filterless watch "
                "would fire on everything (if this is a recurring task, create a "
                "routine schedule instead)",
            }
        condition_type = "event"
        compiled = {"match": "events", "filters": [f.model_dump() for f in out.filters]}

    async with get_session_factory()() as session:
        from app.auth import current_user_id

        intent = StandingIntent(
            user_id=current_user_id(),
            text=text[:2000],
            condition_type=condition_type,
            compiled=compiled,
            semantic_predicate=out.semantic_predicate,
            base_interval_s=max(out.cadence_s, 60),
            current_interval_s=max(out.cadence_s, 60),
            status="proposed",
        )
        session.add(intent)
        await session.commit()
        await session.refresh(intent)
    return {
        "status": "proposed",
        "intent_id": str(intent.id),
        "interpretation": out.echo,
        "compiled": compiled,
        "note": "ask the user to confirm, then call ambient.confirm_watch with this intent_id",
    }
