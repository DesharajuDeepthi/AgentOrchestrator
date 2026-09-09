# ADR-0007: Route OpenAI reasoning-effort runs through the Responses API

Status: Accepted

Date: 2026-08-07

## Context

The stage-19 acceptance campaign (docs/acceptance/19-provider-agnostic/)
drove the whole system on `openai:gpt-5.6-terra` with reasoning effort set
through the normalized `ModelParams` (ADR-0002). It immediately flushed out a
real provider-behavior bug — exactly what the campaign existed to find:
current OpenAI reasoning models **reject the combination of function tools
and `reasoning_effort` on `/v1/chat/completions`**. Every effort-bearing run
that also bound tools (which is every orchestrator loop) failed at the
provider boundary, while the same code was fine on Anthropic and Gemini.

## Decision

Absorb the quirk entirely inside the OpenAI adapter
(`backend/app/llm/adapters.py`, `OpenAIProvider.get_chat_model`). When
`params.effort` is set, the adapter constructs `ChatOpenAI` with:

```python
kwargs["use_responses_api"] = True
kwargs["output_version"] = "responses/v1"
kwargs["reasoning"] = {"effort": _OPENAI_REASONING_EFFORT[params.effort]}
```

so effort-bearing runs ride the **Responses API** instead of Chat
Completions, with `output_version="responses/v1"` normalizing the message
shape. The normalized effort levels map via
`{"none": "minimal", "low": "low", "medium": "medium", "high": "high"}`.
Runs without effort continue to use plain Chat Completions. Non-reasoning
models (`gpt-4o`) declare `supports_effort=False` in `ModelInfo` and are
rejected at save if effort is selected; the reasoning family declares
`supports_temperature=False` for the symmetric reason.

The return type is the same `BaseChatModel`, so **zero consumer changes**:
planner, middlewares, skill loops, and aggregator never learned the
Responses API exists. The adapter contract test suite was updated to cover
the routing.

## Consequences

Positive:

- Validated the entire point of ADR-0002: a provider-specific transport
  quirk was fixed in one adapter method, one commit (`2fc0615`), with the
  rest of the codebase untouched.
- Reasoning + tools works on OpenAI models, proven by 20/20 completed
  campaign turns after the fix.
- The `ModelInfo` capability flags turn provider param rules into 422s at
  save time instead of runtime provider errors.

Negative:

- One provider now has two wire protocols selected by a parameter; debugging
  must first establish which API a given run used.
- The routing rule encodes today's OpenAI behavior — if Chat Completions
  later accepts tools + reasoning effort, or Responses semantics drift, the
  adapter (and its contract tests) must track it.
- `output_version="responses/v1"` couples to LangChain's normalization of
  Responses payloads; upstream changes there hit this path first.

## References

- /home/user/concierge-agent/backend/app/llm/adapters.py (OpenAIProvider)
- /home/user/concierge-agent/docs/acceptance/README.md (stage 19 findings)
- Commit `2fc0615` — fix(llm): route OpenAI reasoning-effort runs through
  the Responses API
- Related: ADR-0002 (provider port and contract tests)
