"""Native tool / sub agent provider (spec §5b, §3.4).

Code-defined entries register at startup via decorators; the scan upserts
them into the registries (kind='native', source='static'). Guardrails are
enforced at registration: no HITL inside native subgraphs, no registry
sub agent wrapping.
"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, get_type_hints

from pydantic import create_model

NativeFn = Callable[..., Awaitable[Any]]


class NativeGuardrailError(ValueError):
    """A native registration violates a §5b guardrail."""


@dataclass
class NativeToolEntry:
    name: str
    description: str
    fn: NativeFn
    native_ref: str
    input_schema: dict[str, Any]


@dataclass
class NativeSubAgentEntry:
    name: str
    description: str
    build: Callable[[Any], Any]  # (checkpointer) -> compiled StateGraph
    native_ref: str
    covers_skill_ids: list[str] = field(default_factory=list)


_NATIVE_TOOLS: dict[str, NativeToolEntry] = {}
_NATIVE_SUB_AGENTS: dict[str, NativeSubAgentEntry] = {}

# Heuristic source markers for the two structural guardrails. POC-grade static
# analysis: interrupts and registry-sub-agent dispatch have exactly these
# entry points in this codebase.
_INTERRUPT_MARKERS = ("interrupt(",)
_SUB_AGENT_DISPATCH_MARKERS = ("dispatch_sub_agent(", "invoke_sub_agent(", "build_worker(")


def _check_guardrails(fn: Callable[..., Any], name: str) -> None:
    try:
        source = inspect.getsource(fn)
    except OSError:  # pragma: no cover - builtins/C callables
        source = ""
    for marker in _INTERRUPT_MARKERS:
        if marker in source:
            raise NativeGuardrailError(
                f"native tool {name!r}: interrupt() is not allowed inside a native "
                "subgraph — LangGraph interrupts do not propagate out of a tool call"
            )
    for marker in _SUB_AGENT_DISPATCH_MARKERS:
        if marker in source:
            raise NativeGuardrailError(
                f"native tool {name!r}: native tools may not invoke registry sub agents "
                "(prevents sub agent → skill → tool → sub agent cycles)"
            )


def derive_input_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """JSON Schema from the callable signature (spec §5b)."""
    hints = get_type_hints(fn)
    hints.pop("return", None)
    sig = inspect.signature(fn)
    fields: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if pname in {"self", "config", "callbacks"}:
            continue
        annotation = hints.get(pname, str)
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[pname] = (annotation, default)
    model = create_model(f"{fn.__name__}_input", **fields)
    schema: dict[str, Any] = model.model_json_schema()
    return schema


def native_tool(name: str, description: str) -> Callable[[NativeFn], NativeFn]:
    def decorator(fn: NativeFn) -> NativeFn:
        _check_guardrails(fn, name)
        _NATIVE_TOOLS[name] = NativeToolEntry(
            name=name,
            description=description,
            fn=fn,
            native_ref=f"{fn.__module__}.{fn.__qualname__}",
            input_schema=derive_input_schema(fn),
        )
        return fn

    return decorator


def native_sub_agent(
    name: str, description: str, covers_skill_ids: list[str] | None = None
) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
    """Register a native sub agent: `build(checkpointer)` must return a
    compiled graph over the standard state schema (spec §3.4)."""

    def decorator(build: Callable[[Any], Any]) -> Callable[[Any], Any]:
        _NATIVE_SUB_AGENTS[name] = NativeSubAgentEntry(
            name=name,
            description=description,
            build=build,
            native_ref=f"{build.__module__}.{build.__qualname__}",
            covers_skill_ids=list(covers_skill_ids or []),
        )
        return build

    return decorator


def native_tools() -> dict[str, NativeToolEntry]:
    return dict(_NATIVE_TOOLS)


def native_sub_agents() -> dict[str, NativeSubAgentEntry]:
    return dict(_NATIVE_SUB_AGENTS)


def clear_registrations() -> None:
    """Testing hook."""
    _NATIVE_TOOLS.clear()
    _NATIVE_SUB_AGENTS.clear()


def scan_native() -> None:
    """Import modules under app/native so decorators run (idempotent)."""
    import app.native.agents  # noqa: F401
    import app.native.ambient_tools  # noqa: F401
    import app.native.memory_tools  # noqa: F401
    import app.native.tools  # noqa: F401
