"""Provider registry + the single entry point get_model() (spec §2.1).

Adapters are code-registered via the @model_provider decorator, keyed by
provider_id. Every LLM call in the system resolves through get_model().
"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.llm.port import ModelParams, ModelProvider

_REGISTRY: dict[str, ModelProvider] = {}


class UnknownProviderError(ValueError):
    """Model reference names a provider that is not registered."""


def model_provider[T: type[Any]](cls: T) -> T:
    """Class decorator: instantiate and register the adapter by provider_id."""
    instance = cls()
    _REGISTRY[instance.provider_id] = instance
    return cls


def get_provider(provider_id: str) -> ModelProvider:
    try:
        return _REGISTRY[provider_id]
    except KeyError as exc:
        raise UnknownProviderError(
            f"unknown model provider {provider_id!r}; registered: {sorted(_REGISTRY)}"
        ) from exc


def list_providers() -> list[ModelProvider]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split a 'provider:model' reference string."""
    provider_id, sep, model = ref.partition(":")
    if not sep or not provider_id or not model:
        raise UnknownProviderError(f"model reference {ref!r} is not 'provider:model'")
    return provider_id, model


def get_model(ref: str, params: ModelParams | None = None) -> BaseChatModel:
    """Resolve 'provider:model' against the registry and delegate to the adapter."""
    provider_id, model = parse_model_ref(ref)
    return get_provider(provider_id).get_chat_model(model, params)


def validate_model_selection(ref: str, params: ModelParams | None = None) -> list[str]:
    """Save-time validation (spec §2.1): unconfigured provider, unknown model,
    or an unsupported param combination all reject with the returned errors."""
    try:
        provider_id, model = parse_model_ref(ref)
        provider = get_provider(provider_id)
    except UnknownProviderError as exc:
        return [str(exc)]
    errors: list[str] = []
    if not provider.is_configured():
        errors.append(f"provider {provider_id!r} is not configured (API key env var missing)")
    info = next((m for m in provider.list_models() if m.id == model), None)
    if info is None:
        errors.append(f"model {model!r} is not in provider {provider_id!r}'s model list")
    else:
        unsupported = info.unsupported_params(params)
        if unsupported:
            errors.append(f"model {model!r} does not support params: {', '.join(unsupported)}")
    return errors


async def get_embeddings(ref: str, texts: list[str]) -> list[list[float]]:
    """Single embeddings entry point (spec §2.1): 'provider:model' → vectors."""
    provider_id, model = parse_model_ref(ref)
    return await get_provider(provider_id).get_embeddings(model, texts)


def validate_embedding_selection(ref: str) -> list[str]:
    """Save-time validation for `embedding_model` (spec §3.7)."""
    try:
        provider_id, _model = parse_model_ref(ref)
        provider = get_provider(provider_id)
    except UnknownProviderError as exc:
        return [str(exc)]
    errors: list[str] = []
    if not provider.is_configured():
        errors.append(f"provider {provider_id!r} is not configured (API key env var missing)")
    if not provider.supports_embeddings():
        errors.append(f"provider {provider_id!r} has no embeddings API")
    return errors


def register_builtin_providers() -> None:
    """Import adapter modules so their decorators run (idempotent)."""
    import app.llm.adapters  # noqa: F401
    import app.llm.fake  # noqa: F401
