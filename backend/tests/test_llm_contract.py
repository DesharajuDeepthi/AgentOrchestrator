"""Shared adapter contract suite (spec §2.1).

Every registered adapter must pass the port-shape contract. Behavioral
round-trips (tool calling, structured output, usage metadata) are proven
through the port with the fake provider — tests never touch a provider SDK
network path (spec §11).
"""

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from app.config import get_config
from app.llm import (
    ModelInfo,
    ModelParams,
    ModelProvider,
    ProviderNotConfiguredError,
    UnknownProviderError,
    get_model,
    get_provider,
    list_providers,
    validate_model_selection,
)
from app.llm import fake as fake_llm

ALL_PROVIDERS = list_providers()
PROVIDER_IDS = [p.provider_id for p in ALL_PROVIDERS]


@pytest.mark.parametrize("provider", ALL_PROVIDERS, ids=PROVIDER_IDS)
def test_port_shape(provider: ModelProvider) -> None:
    assert isinstance(provider.provider_id, str) and provider.provider_id
    assert isinstance(provider.is_configured(), bool)
    models = provider.list_models()
    assert isinstance(models, list) and models
    assert all(isinstance(m, ModelInfo) for m in models)


@pytest.mark.parametrize("provider", ALL_PROVIDERS, ids=PROVIDER_IDS)
def test_unconfigured_provider_refuses_model(
    provider: ModelProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    if provider.provider_id == "fake":
        monkeypatch.setenv("FAKE_LLM_ENABLED", "0")
    get_config.cache_clear()
    try:
        assert provider.is_configured() is False
        with pytest.raises(ProviderNotConfiguredError):
            provider.get_chat_model(provider.list_models()[0].id)
    finally:
        get_config.cache_clear()


@pytest.mark.parametrize("provider", ALL_PROVIDERS, ids=PROVIDER_IDS)
def test_configured_provider_returns_base_chat_model(
    provider: ModelProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-dummy")
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-dummy")
    monkeypatch.setenv("CUSTOM_GATEWAY_BASE_URL", "https://llm-gw.corp.example/v1")
    monkeypatch.setenv("CUSTOM_GATEWAY_API_KEY", "test-dummy")
    monkeypatch.setenv("CUSTOM_GATEWAY_MODELS", "corp-large")
    get_config.cache_clear()
    try:
        assert provider.is_configured() is True
        model = provider.get_chat_model(provider.list_models()[0].id)
        assert isinstance(model, BaseChatModel)
    finally:
        get_config.cache_clear()


def test_registered_builtins_present() -> None:
    assert {"anthropic", "google_genai", "openai", "openrouter", "custom", "fake"} <= set(
        PROVIDER_IDS
    )


def test_get_model_resolves_prefix() -> None:
    model = get_model("fake:scripted")
    assert isinstance(model, BaseChatModel)


@pytest.mark.parametrize("ref", ["nosuch:model", "noseparator", ":model", "prov:"])
def test_bad_model_refs_rejected(ref: str) -> None:
    with pytest.raises(UnknownProviderError):
        get_model(ref)


def test_tool_calling_round_trip_through_port() -> None:
    from langchain_core.tools import tool

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    model = get_model("fake:scripted").bind_tools([add])
    fake_llm.push_ai("", tool_calls=[{"name": "add", "args": {"a": 2, "b": 3}, "id": "tc1"}])
    result: Any = model.invoke("add 2 and 3")
    assert result.tool_calls and result.tool_calls[0]["name"] == "add"
    tool_msg = add.invoke(result.tool_calls[0])
    assert "5" in str(tool_msg.content)


def test_structured_output_through_port() -> None:
    class Verdict(BaseModel):
        answer: str
        confident: bool

    model = get_model("fake:scripted").with_structured_output(Verdict)
    fake_llm.push_ai(
        "",
        tool_calls=[{"name": "Verdict", "args": {"answer": "yes", "confident": True}, "id": "tc1"}],
    )
    result = model.invoke("verdict?")
    assert isinstance(result, Verdict)
    assert result.answer == "yes" and result.confident is True


def test_usage_metadata_populated_through_port() -> None:
    model = get_model("fake:scripted")
    fake_llm.push_ai("hello")
    msg: Any = model.invoke("hi")
    assert msg.usage_metadata is not None
    assert msg.usage_metadata["input_tokens"] > 0
    assert msg.usage_metadata["output_tokens"] > 0


def test_get_provider_unknown() -> None:
    with pytest.raises(UnknownProviderError):
        get_provider("does-not-exist")


class TestModelParamsMapping:
    """Normalized ModelParams mapping incl. effort→provider knob (spec §2.1)."""

    @pytest.fixture(autouse=True)
    def _dummy_keys(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-dummy")
        monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-dummy")
        get_config.cache_clear()
        yield
        get_config.cache_clear()

    def test_anthropic_effort_maps_to_thinking_budget(self) -> None:
        model = get_model(
            "anthropic:claude-sonnet-4-6",
            ModelParams(effort="high", temperature=0.2, max_output_tokens=2000),
        )
        thinking = getattr(model, "thinking", None)
        assert thinking is not None and thinking["type"] == "enabled"
        assert thinking["budget_tokens"] == 32768
        assert model.temperature == 0.2
        assert model.max_tokens == 2000

    def test_claude5_effort_maps_to_adaptive_output_config(self) -> None:
        """Claude 5 family uses adaptive thinking + output_config.effort —
        the legacy budgeted-thinking knob is rejected by the API."""
        model = get_model("anthropic:claude-sonnet-5", ModelParams(effort="medium"))
        thinking = getattr(model, "thinking", None)
        assert thinking is not None and thinking["type"] == "adaptive"
        assert "budget_tokens" not in thinking
        output_config = getattr(model, "output_config", None)
        assert output_config == {"effort": "medium"}

    def test_anthropic_effort_none_disables_thinking(self) -> None:
        model = get_model("anthropic:claude-sonnet-4-6", ModelParams(effort="none"))
        assert getattr(model, "thinking", None) is None

    def test_openai_effort_maps_to_reasoning_effort(self) -> None:
        # reasoning effort rides the Responses API (chat/completions rejects
        # function tools + reasoning_effort on current reasoning models)
        model = get_model("openai:gpt-5", ModelParams(effort="medium", max_output_tokens=512))
        assert model.use_responses_api is True
        assert model.reasoning == {"effort": "medium"}

    def test_openai_effort_none_maps_to_minimal(self) -> None:
        model = get_model("openai:gpt-5", ModelParams(effort="none"))
        assert model.use_responses_api is True
        assert model.reasoning == {"effort": "minimal"}

    def test_gemini_effort_maps_to_thinking_budget(self) -> None:
        model = get_model("google_genai:gemini-2.5-flash", ModelParams(effort="low"))
        assert model.thinking_budget == 1024

    def test_fake_records_params(self) -> None:
        model = get_model(
            "fake:scripted", ModelParams(effort="low", temperature=0.5, max_output_tokens=64)
        )
        assert model.effort == "low"
        assert model.temperature == 0.5
        assert model.max_output_tokens == 64

    def test_unsupported_combination_rejected_at_adapter(self) -> None:
        # gpt-5 is a reasoning model: temperature is not supported
        # (the effort-rejection branch of _check_params is the same code
        # path; its former subject gpt-4o left the list — see adapters.py)
        with pytest.raises(ValueError, match="temperature"):
            get_model("openai:gpt-5", ModelParams(temperature=0.3))


class TestValidateModelSelection:
    def test_ok(self) -> None:
        assert validate_model_selection("fake:scripted", ModelParams(effort="high")) == []

    def test_unconfigured_provider(self) -> None:
        errors = validate_model_selection("anthropic:claude-sonnet-4-6")
        assert errors and "not configured" in errors[0]

    def test_unknown_model(self) -> None:
        errors = validate_model_selection("fake:nope")
        assert any("not in provider" in e for e in errors)

    def test_unknown_provider(self) -> None:
        errors = validate_model_selection("nosuch:model")
        assert errors and "unknown model provider" in errors[0]

    def test_unsupported_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-dummy")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-dummy")
        get_config.cache_clear()
        try:
            errors = validate_model_selection("openai:gpt-5", ModelParams(temperature=1.0))
            assert any("does not support" in e for e in errors)
        finally:
            get_config.cache_clear()
