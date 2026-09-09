"""Built-in provider adapters (spec §2.1): anthropic, google_genai, openai, openrouter.

Thin wrappers over LangChain provider packages, each gated by its API key env
var. Each adapter maps the normalized ModelParams onto its provider's knobs —
effort becomes the Anthropic thinking budget, the OpenAI reasoning effort, or
the Gemini thinking budget. A future custom gateway adapter implements the
same port and registers here — zero consumer changes.
"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import get_config
from app.llm.port import (
    EmbeddingsNotSupportedError,
    ModelInfo,
    ModelParams,
    ProviderNotConfiguredError,
)
from app.llm.registry import model_provider

# effort → Anthropic extended-thinking budget tokens
_ANTHROPIC_THINKING_BUDGET = {"low": 1024, "medium": 8192, "high": 32768}
_CLAUDE5_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")
# effort → Gemini thinking budget tokens ('none' disables thinking)
_GEMINI_THINKING_BUDGET = {"none": 0, "low": 1024, "medium": 8192, "high": 24576}
# effort → OpenAI reasoning effort string
_OPENAI_REASONING_EFFORT = {"none": "minimal", "low": "low", "medium": "medium", "high": "high"}
# effort → OpenRouter unified reasoning config (https://openrouter.ai/docs)
_OPENROUTER_REASONING: dict[str, dict[str, Any]] = {
    "none": {"enabled": False},
    "low": {"effort": "low"},
    "medium": {"effort": "medium"},
    "high": {"effort": "high"},
}
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_openrouter_chat_cls: type | None = None


def _openrouter_chat_class() -> type:
    """ChatOpenAI specialized for OpenRouter: structured output defaults to
    function calling — many routed models (GLM, Qwen, DeepSeek) support tools
    but ignore OpenAI's response_format json_schema, which is langchain's
    default method and fails parsing on their prose replies."""
    global _openrouter_chat_cls
    if _openrouter_chat_cls is None:
        from langchain_openai import ChatOpenAI

        class _ChatOpenRouter(ChatOpenAI):
            def with_structured_output(self, schema: Any = None, **kwargs: Any) -> Any:
                kwargs.setdefault("method", "function_calling")
                return super().with_structured_output(schema, **kwargs)

            def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
                # several routed vendors (Z.AI among them) reject forced
                # tool_choice ("Tool choice must be auto") — coerce; a model
                # offered a single tool calls it under auto in practice
                if kwargs.get("tool_choice") not in (None, "auto", "none"):
                    kwargs["tool_choice"] = "auto"
                return super().bind_tools(tools, **kwargs)

        _openrouter_chat_cls = _ChatOpenRouter
    return _openrouter_chat_cls


def _check_params(provider: "ModelProviderBase", model: str, params: ModelParams | None) -> None:
    info = next((m for m in provider.list_models() if m.id == model), None)
    if info is not None:
        unsupported = info.unsupported_params(params)
        if unsupported:
            raise ValueError(
                f"{provider.provider_id}:{model} does not support params: {', '.join(unsupported)}"
            )


class ModelProviderBase:
    provider_id = "base"

    def list_models(self) -> list[ModelInfo]:  # pragma: no cover - overridden
        return []

    # embeddings (spec §2.1/§7.4): opt-in per adapter; default is unsupported
    def supports_embeddings(self) -> bool:
        return False

    async def get_embeddings(self, model: str, texts: list[str]) -> list[list[float]]:
        raise EmbeddingsNotSupportedError(f"provider {self.provider_id!r} has no embeddings API")


@model_provider
class AnthropicProvider(ModelProviderBase):
    provider_id = "anthropic"

    def is_configured(self) -> bool:
        return bool(get_config().anthropic_api_key)

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo("claude-sonnet-5", "Claude Sonnet 5"),
            ModelInfo("claude-opus-5", "Claude Opus 5"),
            ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ModelInfo("claude-opus-4-6", "Claude Opus 4.6"),
            ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5"),
        ]

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel:
        if not self.is_configured():
            raise ProviderNotConfiguredError("anthropic: ANTHROPIC_API_KEY not set")
        _check_params(self, model, params)
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {}
        if params:
            if params.effort and params.effort != "none":
                if model.startswith(_CLAUDE5_PREFIXES):
                    # the Claude 5 family replaces budgeted thinking with
                    # adaptive thinking steered by output_config.effort
                    kwargs["thinking"] = {"type": "adaptive"}
                    kwargs["output_config"] = {"effort": params.effort}
                else:
                    kwargs["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": _ANTHROPIC_THINKING_BUDGET[params.effort],
                    }
            if params.temperature is not None:
                kwargs["temperature"] = params.temperature
            if params.max_output_tokens is not None:
                kwargs["max_tokens"] = params.max_output_tokens
        return ChatAnthropic(model_name=model, **kwargs)


@model_provider
class GoogleGenAIProvider(ModelProviderBase):
    provider_id = "google_genai"

    def is_configured(self) -> bool:
        return bool(get_config().google_api_key)

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo("gemini-3.6-flash", "Gemini 3.6 Flash"),
            ModelInfo("gemini-3.5-flash", "Gemini 3.5 Flash"),
            ModelInfo("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite"),
            ModelInfo("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ModelInfo("gemini-2.5-flash", "Gemini 2.5 Flash"),
        ]

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel:
        if not self.is_configured():
            raise ProviderNotConfiguredError("google_genai: GOOGLE_API_KEY not set")
        _check_params(self, model, params)
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs: dict[str, Any] = {}
        if params:
            if params.effort is not None:
                kwargs["thinking_budget"] = _GEMINI_THINKING_BUDGET[params.effort]
            if params.temperature is not None:
                kwargs["temperature"] = params.temperature
            if params.max_output_tokens is not None:
                kwargs["max_output_tokens"] = params.max_output_tokens
        return ChatGoogleGenerativeAI(model=model, **kwargs)

    def supports_embeddings(self) -> bool:
        return True

    async def get_embeddings(self, model: str, texts: list[str]) -> list[list[float]]:
        if not self.is_configured():
            raise ProviderNotConfiguredError("google_genai: GOOGLE_API_KEY not set")
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return await GoogleGenerativeAIEmbeddings(model=model).aembed_documents(texts)


@model_provider
class OpenAIProvider(ModelProviderBase):
    provider_id = "openai"

    def is_configured(self) -> bool:
        return bool(get_config().openai_api_key)

    def list_models(self) -> list[ModelInfo]:
        return [
            # reasoning family: effort supported, temperature not accepted
            ModelInfo("gpt-5.6-terra", "GPT-5.6 Terra", supports_temperature=False),
            ModelInfo("gpt-5.6-sol", "GPT-5.6 Sol", supports_temperature=False),
            ModelInfo("gpt-5.6-luna", "GPT-5.6 Luna", supports_temperature=False),
            ModelInfo("gpt-5.5", "GPT-5.5", supports_temperature=False),
            ModelInfo("gpt-5.4", "GPT-5.4", supports_temperature=False),
            ModelInfo("gpt-5.4-mini", "GPT-5.4 mini", supports_temperature=False),
            ModelInfo("gpt-5", "GPT-5", supports_temperature=False),
            ModelInfo("gpt-5-mini", "GPT-5 mini", supports_temperature=False),
            # gpt-4o removed: OpenAI's /v1/chat/completions now rejects
            # function tools for it outright when reasoning params appear,
            # and it cannot ride the Responses-API path (no effort knob) —
            # every remaining entry can
        ]

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel:
        if not self.is_configured():
            raise ProviderNotConfiguredError("openai: OPENAI_API_KEY not set")
        _check_params(self, model, params)
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {}
        if params:
            if params.effort is not None:
                # current OpenAI reasoning models reject function tools +
                # reasoning_effort on /v1/chat/completions — reasoning runs
                # ride the Responses API instead (same BaseChatModel out)
                kwargs["use_responses_api"] = True
                kwargs["output_version"] = "responses/v1"
                kwargs["reasoning"] = {"effort": _OPENAI_REASONING_EFFORT[params.effort]}
            if params.temperature is not None:
                kwargs["temperature"] = params.temperature
            if params.max_output_tokens is not None:
                kwargs["max_tokens"] = params.max_output_tokens
        return ChatOpenAI(model=model, **kwargs)

    def supports_embeddings(self) -> bool:
        return True

    async def get_embeddings(self, model: str, texts: list[str]) -> list[list[float]]:
        if not self.is_configured():
            raise ProviderNotConfiguredError("openai: OPENAI_API_KEY not set")
        from langchain_openai import OpenAIEmbeddings

        return await OpenAIEmbeddings(model=model).aembed_documents(texts)


@model_provider
class OpenRouterProvider(ModelProviderBase):
    """The spec §2.1 'custom gateway adapter' made real: OpenRouter's
    OpenAI-compatible endpoint through ChatOpenAI with a base_url override.
    Effort maps to OpenRouter's unified `reasoning` config via extra_body.
    Chat-only — no embeddings API (consumers degrade per §7.4)."""

    provider_id = "openrouter"

    def is_configured(self) -> bool:
        return bool(get_config().openrouter_api_key)

    def list_models(self) -> list[ModelInfo]:
        # curated tool-capable subset (any 'openrouter:vendor/model' ref
        # still resolves — the catalog is 400+ models and changes weekly)
        return [
            ModelInfo("z-ai/glm-5.3", "GLM 5.3 (Z.ai)"),
            ModelInfo("z-ai/glm-5.2", "GLM 5.2 (Z.ai)"),
            ModelInfo("deepseek/deepseek-v4-pro-0813", "DeepSeek V4 Pro"),
            ModelInfo("qwen/qwen3.8-max", "Qwen 3.8 Max"),
            ModelInfo("qwen/qwen3.6-plus", "Qwen 3.6 Plus"),
            ModelInfo("moonshotai/kimi-k3", "Kimi K3"),
            ModelInfo("stealth/ox-alpha", "Ox Alpha (stealth preview)"),
        ]

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel:
        config = get_config()
        if not self.is_configured():
            raise ProviderNotConfiguredError("openrouter: OPENROUTER_API_KEY not set")
        _check_params(self, model, params)
        chat_cls = _openrouter_chat_class()

        kwargs: dict[str, Any] = {
            "base_url": _OPENROUTER_BASE_URL,
            "api_key": config.openrouter_api_key,
        }
        extra_body: dict[str, Any] = {}
        if params:
            if params.effort is not None:
                extra_body["reasoning"] = _OPENROUTER_REASONING[params.effort]
            if params.temperature is not None:
                kwargs["temperature"] = params.temperature
            if params.max_output_tokens is not None:
                kwargs["max_tokens"] = params.max_output_tokens
        if extra_body:
            kwargs["extra_body"] = extra_body
        model_obj: BaseChatModel = chat_cls(model=model, **kwargs)
        return model_obj


@model_provider
class CustomGatewayProvider(ModelProviderBase):
    """The enterprise scenario from §2.1, implemented per §18.7: an
    OpenAI-compatible chat-completions gateway behind env-only
    CUSTOM_GATEWAY_BASE_URL / CUSTOM_GATEWAY_API_KEY, with the model list
    from CUSTOM_GATEWAY_MODELS (comma-separated — env keeps list_models()
    sync). Effort is declared unsupported: a generic gateway has no unified
    reasoning knob — an EXPLICIT effort in saved model_params is rejected at
    validation, while the internal role-default effort hints (judge,
    extraction, summaries pass effort='low') are dropped at call time so a
    custom model can serve every role.
    Chat-only — no embeddings API (consumers degrade per §7.4)."""

    provider_id = "custom"

    def is_configured(self) -> bool:
        config = get_config()
        return bool(config.custom_gateway_base_url) and bool(config.custom_gateway_api_key)

    def list_models(self) -> list[ModelInfo]:
        raw = get_config().custom_gateway_models or ""
        ids = [m.strip() for m in raw.split(",") if m.strip()]
        if not ids:
            # port shape: the list is never empty — the placeholder documents
            # how to configure and is rejected by get_chat_model anyway
            return [
                ModelInfo(
                    "gateway-model",
                    "custom gateway (set CUSTOM_GATEWAY_MODELS)",
                    supports_effort=False,
                )
            ]
        return [ModelInfo(i, i, supports_effort=False) for i in ids]

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        config = get_config()
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                "custom: CUSTOM_GATEWAY_BASE_URL / CUSTOM_GATEWAY_API_KEY not set"
            )
        if params is not None and params.effort is not None:
            params = params.model_copy(update={"effort": None})
        _check_params(self, model, params)
        kwargs: dict[str, Any] = {
            "base_url": config.custom_gateway_base_url,
            "api_key": config.custom_gateway_api_key,
        }
        if params:
            if params.temperature is not None:
                kwargs["temperature"] = params.temperature
            if params.max_output_tokens is not None:
                kwargs["max_tokens"] = params.max_output_tokens
        model_obj: BaseChatModel = ChatOpenAI(model=model, **kwargs)
        return model_obj
