"""Scriptable fake provider — how every test injects LLM behavior (spec §11).

Registered through the same @model_provider port as real adapters, so tests
exercise the identical resolution path (`get_model("fake:...")`) without ever
touching a provider SDK. Enabled via FAKE_LLM_ENABLED env.
"""

from collections import deque
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import get_config
from app.llm.port import ModelInfo, ModelParams, ProviderNotConfiguredError
from app.llm.registry import model_provider

_SCRIPT: deque[AIMessage | BaseException] = deque()
_SEEN_TOOLS: list[list[str]] = []

_DEFAULT_USAGE = UsageMetadata(input_tokens=7, output_tokens=11, total_tokens=18)


def push_ai(
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    delay_s: float | None = None,
) -> None:
    """Queue the next scripted response (FIFO across all fake model calls)."""
    msg = AIMessage(content=content, tool_calls=tool_calls or [])
    msg.usage_metadata = _DEFAULT_USAGE
    if delay_s:
        msg.additional_kwargs["__delay_s"] = delay_s
    _SCRIPT.append(msg)


def push_error(exc: BaseException) -> None:
    """Queue an exception: the next fake model call raises it (LLM error path)."""
    _SCRIPT.append(exc)


def seen_tools() -> list[list[str]]:
    """Tool names bound on each captured model call (isolation assertions)."""
    return list(_SEEN_TOOLS)


def clear_seen_tools() -> None:
    _SEEN_TOOLS.clear()


def push_message(msg: AIMessage) -> None:
    if msg.usage_metadata is None:
        msg.usage_metadata = _DEFAULT_USAGE
    _SCRIPT.append(msg)


def clear_script() -> None:
    _SCRIPT.clear()
    _SEEN_TOOLS.clear()


def script_len() -> int:
    return len(_SCRIPT)


class ScriptedChatModel(BaseChatModel):
    """Pops scripted AIMessages; falls back to a canned text answer.

    Records the normalized params it was constructed with so the adapter
    contract suite can assert the params → model mapping through the port.
    """

    model_name: str = "scripted"
    effort: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        bound_tools = kwargs.get("tools") or []
        _SEEN_TOOLS.append(
            [t.get("function", {}).get("name", t.get("name", "?")) for t in bound_tools]
        )
        if _SCRIPT:
            item = _SCRIPT.popleft()
            if isinstance(item, BaseException):
                raise item
            msg = item
            delay = msg.additional_kwargs.pop("__delay_s", None)
            if delay:
                import time

                time.sleep(float(delay))
        else:
            msg = self._default_message(_SEEN_TOOLS[-1] if _SEEN_TOOLS else [])
        # real providers stamp model_name; usage callbacks key on it
        msg.response_metadata.setdefault("model_name", f"fake:{self.model_name}")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _default_message(self, tool_names: list[str]) -> AIMessage:
        """Unscripted default: satisfy known structured-output schemas so an
        unscripted server (curl demos, keyless compose) still completes runs."""
        if "PlannerOutput" in tool_names:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "PlannerOutput",
                        "args": {
                            "entries": [],
                            "direct_answer": f"fake-answer[{self.model_name}]",
                            "no_confident_match": False,
                        },
                        "id": "fake-plan",
                    }
                ],
            )
        elif "ConditionChoice" in tool_names:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "ConditionChoice", "args": {"index": 0}, "id": "fake-route"}],
            )
        elif "OverlapVerdict" in tool_names:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "OverlapVerdict",
                        "args": {
                            "overlap_percent": 0,
                            "match_type": "none",
                            "match_id": None,
                            "match_name": None,
                            "reasoning": "unscripted default",
                        },
                        "id": "fake-overlap",
                    }
                ],
            )
        elif "AnswerUi" in tool_names:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "AnswerUi", "args": {"components": []}, "id": "fake-ui"}],
            )
        else:
            msg = AIMessage(content=f"fake-answer[{self.model_name}]")
        msg.usage_metadata = _DEFAULT_USAGE
        return msg


@model_provider
class FakeProvider:
    provider_id = "fake"

    def is_configured(self) -> bool:
        return get_config().fake_llm_enabled

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("scripted", "Scripted fake model")]

    def get_chat_model(self, model: str, params: ModelParams | None = None) -> BaseChatModel:
        if not self.is_configured():
            raise ProviderNotConfiguredError("fake: FAKE_LLM_ENABLED not set")
        return ScriptedChatModel(
            model_name=model,
            effort=params.effort if params else None,
            temperature=params.temperature if params else None,
            max_output_tokens=params.max_output_tokens if params else None,
        )

    def supports_embeddings(self) -> bool:
        return True

    async def get_embeddings(self, model: str, texts: list[str]) -> list[list[float]]:
        """Deterministic bag-of-tokens vectors: shared tokens produce real
        cosine similarity, so ranking tests exercise genuine vector math."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("fake: FAKE_LLM_ENABLED not set")
        dims = 64
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dims
            for token in text.lower().split():
                vec[hash(token) % dims] += 1.0
            out.append(vec)
        return out
