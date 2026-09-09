# ADR-0002: ModelProvider port and adapter registry for all model access

Status: Accepted

Date: 2026-08-04

## Context

A custom enterprise provider adapter (its own gateway, its own model list)
will be added post-POC. It must plug in without touching any consumer code.
The usual failure mode is gradual: the "main" provider's SDK leaks into
planners, tools, and UI code because it is always configured, and the
abstraction only ever gets exercised by the providers nobody uses.

## Decision

A first-class provider layer lives in `backend/app/llm/` and is used for
**every** provider, including Anthropic — never bypassed, never special-cased
(spec §2.1, restated as a hard constraint in CLAUDE.md).

- **Port**: `ModelProvider` protocol (`backend/app/llm/port.py`) —
  `provider_id`, `is_configured()`, `list_models()`,
  `get_chat_model(model, params)`, plus `supports_embeddings()` /
  `get_embeddings()` for retrieval (ADR-0005, ADR-0006).
- **Normalized params**: `ModelParams` carries `effort | temperature |
  max_output_tokens`; each adapter maps `effort` onto its provider's knob
  (Anthropic thinking budget or adaptive thinking, OpenAI reasoning effort,
  Gemini thinking budget). `ModelInfo` declares which params a model
  supports; unsupported combinations are rejected at save (422).
- **Single entry point**: `get_model("provider:model")` resolves the prefix
  against a decorator-populated adapter registry
  (`backend/app/llm/registry.py`). Every LLM call — planner, router,
  aggregator, skill loops, native tools — goes through it.
- **No leakage**: no provider SDK or LangChain provider package import
  outside `app/llm/`. The common currency downstream is LangChain's
  `BaseChatModel`; structured outputs via LangChain abstractions only; token
  accounting via `usage_metadata` only; prompts provider-neutral.
- **Contract tests**: one shared pytest suite asserts the port contract
  (list/configure/get, effort mapping, tool-calling round trip, structured
  output, usage metadata) and every registered adapter must pass it.

The abstraction was proven, not just asserted: real acceptance campaigns ran
the identical system on `openai:gpt-5.6-terra` (5 conversations × both
orchestrator modes, 20/20 turns) and on `google_genai:gemini-3.5-flash`,
with zero consumer-code changes — only the Settings model select
(docs/acceptance stages 19–20). Stage 20 mixed three providers inside single
runs (Anthropic orchestrator, OpenAI planner or sub-agent override).

## Consequences

Positive:

- The future gateway adapter is "implement the port, register, done."
- Per-role and per-record model overrides (planner, aggregator, sub agent,
  skill) are just `provider:model` strings — heterogeneous runs work today.
- Provider quirks stay inside one file per provider; the Responses API
  routing fix (ADR-0007) changed adapters.py and nothing else.

Negative:

- Normalized `ModelParams` is a lowest-common-denominator: provider-specific
  features (e.g. Anthropic `output_config` subtleties, OpenAI verbosity)
  need adapter-side mapping decisions rather than direct exposure.
- Every adapter change must keep the shared contract suite green, which adds
  friction when a provider's LangChain package shifts behavior.
- Indirection cost: debugging a model call means one extra hop through the
  registry before reaching the SDK.

## References

- spec.md §2.1 (non-negotiable basic design); CLAUDE.md "Hard constraints"
- /home/user/concierge-agent/backend/app/llm/port.py, registry.py, adapters.py
- /home/user/concierge-agent/docs/acceptance/19-provider-agnostic/,
  20-heterogeneous-models/
- Related: ADR-0006 (embeddings on the port), ADR-0007 (Responses API routing)
