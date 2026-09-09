"""The ModelProvider port (spec §2.1) — the common currency is BaseChatModel.

``ModelParams`` is the normalized model configuration; each adapter maps
``effort`` to its provider's knob (Anthropic thinking budget, OpenAI reasoning
effort, Gemini thinking config). ``ModelInfo`` declares which params each
model supports; selecting an unsupported combination is rejected at save.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict

Effort = Literal["none", "low", "medium", "high"]


class ModelParams(BaseModel):
    """Normalized model configuration (spec §2.1)."""

    model_config = ConfigDict(extra="forbid")

    effort: Effort | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    supports_effort: bool = True
    supports_temperature: bool = True
    supports_max_output_tokens: bool = True

    def unsupported_params(self, params: ModelParams | None) -> list[str]:
        """Names of params set in `params` that this model does not support."""
        if params is None:
            return []
        errors: list[str] = []
        if params.effort is not None and not self.supports_effort:
            errors.append("effort")
        if params.temperature is not None and not self.supports_temperature:
            errors.append("temperature")
        if params.max_output_tokens is not None and not self.supports_max_output_tokens:
            errors.append("max_output_tokens")
        return errors


class ProviderNotConfiguredError(RuntimeError):
    """Raised when get_chat_model is called on an unconfigured provider."""


class EmbeddingsNotSupportedError(RuntimeError):
    """Raised when get_embeddings is called on a provider without an
    embeddings API (spec §2.1 — consumers degrade to lexical-only)."""


class UnsupportedParamsError(ValueError):
    """Raised when params include options the selected model does not support."""


@runtime_checkable
class ModelProvider(Protocol):
    """A provider adapter's only job is to return a BaseChatModel."""

    provider_id: str

    def is_configured(self) -> bool: ...

    def list_models(self) -> list[ModelInfo]: ...

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel: ...

    def supports_embeddings(self) -> bool: ...

    async def get_embeddings(self, model: str, texts: list[str]) -> list[list[float]]: ...
