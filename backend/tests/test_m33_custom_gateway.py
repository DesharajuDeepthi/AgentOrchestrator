"""M33 — the custom gateway adapter (spec §18.7): OpenAI-compatible
chat-completions gateway behind env-only base-url/key with an env-validated
model list. Registered like every provider; zero changes outside app/llm/."""

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from app.config import get_config
from app.llm import (
    ModelParams,
    ProviderNotConfiguredError,
    get_provider,
    list_providers,
    validate_model_selection,
)


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_GATEWAY_BASE_URL", "https://llm-gw.corp.example/v1")
    monkeypatch.setenv("CUSTOM_GATEWAY_API_KEY", "test-dummy")
    monkeypatch.setenv("CUSTOM_GATEWAY_MODELS", "corp-large, corp-small ,corp-embed-chat")
    get_config.cache_clear()


def test_custom_provider_is_registered() -> None:
    assert "custom" in [p.provider_id for p in list_providers()]


def test_unconfigured_refuses_and_keeps_port_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CUSTOM_GATEWAY_BASE_URL", "CUSTOM_GATEWAY_API_KEY", "CUSTOM_GATEWAY_MODELS"):
        monkeypatch.delenv(var, raising=False)
    get_config.cache_clear()
    try:
        provider = get_provider("custom")
        assert provider.is_configured() is False
        models = provider.list_models()
        assert models  # port shape: never an empty list — a placeholder shows how to configure
        with pytest.raises(ProviderNotConfiguredError):
            provider.get_chat_model(models[0].id)
    finally:
        get_config.cache_clear()


def test_env_model_list_is_the_validated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    try:
        provider = get_provider("custom")
        assert provider.is_configured() is True
        ids = [m.id for m in provider.list_models()]
        assert ids == ["corp-large", "corp-small", "corp-embed-chat"]  # trimmed, ordered
        assert validate_model_selection("custom:corp-large") == []
        errors = validate_model_selection("custom:gpt-x")
        assert errors and "model list" in errors[0]
    finally:
        get_config.cache_clear()


def test_chat_model_wires_base_url_and_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    try:
        provider = get_provider("custom")
        model = provider.get_chat_model(
            "corp-large", ModelParams(temperature=0.2, max_output_tokens=512)
        )
        assert isinstance(model, BaseChatModel)
        assert "llm-gw.corp.example" in str(getattr(model, "openai_api_base", ""))
        assert getattr(model, "temperature", None) == 0.2
    finally:
        get_config.cache_clear()


def test_effort_is_rejected_at_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic gateway has no unified reasoning knob — effort is declared
    unsupported so the save-time validator rejects it instead of silently
    dropping it."""
    _configure(monkeypatch)
    try:
        errors = validate_model_selection("custom:corp-large", ModelParams(effort="high"))
        assert errors and "effort" in errors[0]
    finally:
        get_config.cache_clear()


def test_no_embeddings_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    try:
        assert get_provider("custom").supports_embeddings() is False
    finally:
        get_config.cache_clear()
