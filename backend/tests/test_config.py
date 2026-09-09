"""AppConfig env parsing: compose passes `${VAR:-}`, so any variable absent
from .env reaches the process as an EMPTY STRING — that must mean "unset"
(field default), never a parse error or a falsely-configured provider."""

import pytest

from app.config import AppConfig


def test_blank_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FAKE_LLM_ENABLED",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "LANGSMITH_API_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "BACKEND_PORT",
        "LOG_LEVEL",
    ):
        monkeypatch.setenv(var, "")
    cfg = AppConfig(_env_file=None)
    assert cfg.fake_llm_enabled is False
    assert cfg.anthropic_api_key is None  # "" must not count as configured
    assert cfg.google_api_key is None
    assert cfg.backend_port == 8000
    assert cfg.log_level == "INFO"


def test_real_values_still_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LLM_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("BACKEND_PORT", "9000")
    cfg = AppConfig(_env_file=None)
    assert cfg.fake_llm_enabled is True
    assert cfg.anthropic_api_key == "sk-ant-x"
    assert cfg.backend_port == 9000
