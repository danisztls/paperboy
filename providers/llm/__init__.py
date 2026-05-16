import logging
from typing import TypeVar

from pydantic import BaseModel

from providers.llm.base import LLMAdapter, LLMResponse

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class FallbackAdapter(LLMAdapter):
    """Tries each (adapter, model, default_reasoning) entry in order, falling back on None return.

    Each entry carries its own default reasoning level. When the caller passes a
    truthy `reasoning` value it overrides every entry (used by `--analysis`).
    A falsy / None value lets each entry use its own default.
    """

    def __init__(
        self, entries: list[tuple[LLMAdapter, str | None, str | bool | dict | None]]
    ) -> None:
        self._entries = entries

    def _effective_entries(
        self, model: str | None
    ) -> list[tuple[LLMAdapter, str | None, str | bool | dict | None]]:
        if model is not None:
            head = self._entries[0]
            return [(head[0], model, head[2]), *self._entries[1:]]
        return self._entries

    @staticmethod
    def _resolve_reasoning(
        caller: bool | str | dict, entry_default: str | bool | dict | None
    ) -> bool | str | dict:
        if caller:
            return caller
        return entry_default or False

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | str | dict = False,
    ) -> LLMResponse | None:
        entries = self._effective_entries(model)
        for i, (adapter, effective_model, entry_reasoning) in enumerate(entries):
            result = await adapter.complete(
                prompt,
                model=effective_model,
                instructions=instructions,
                web_search=web_search,
                reasoning=self._resolve_reasoning(reasoning, entry_reasoning),
            )
            if result is not None:
                return result
            if i < len(entries) - 1:
                log.warning(
                    "LLM %s (model=%s) returned None, trying next fallback",
                    type(adapter).__name__,
                    effective_model,
                )
        return None

    async def complete_structured(
        self,
        prompt: str,
        response_model: type[T],
        *,
        model: str | None = None,
        instructions: str | None = None,
        reasoning: bool | str | dict = False,
        trace: dict | None = None,
    ) -> T | None:
        entries = self._effective_entries(model)
        for i, (adapter, effective_model, entry_reasoning) in enumerate(entries):
            result = await adapter.complete_structured(
                prompt,
                response_model,
                model=effective_model,
                instructions=instructions,
                reasoning=self._resolve_reasoning(reasoning, entry_reasoning),
                trace=trace,
            )
            if result is not None:
                return result
            if i < len(entries) - 1:
                log.warning(
                    "LLM %s (model=%s) returned None on structured call, trying next fallback",
                    type(adapter).__name__,
                    effective_model,
                )
        return None


def get_adapter(provider: str | None = None, api_key: str | None = None) -> LLMAdapter:
    if provider == "openai":
        from providers.llm.openai import OpenAIAdapter

        return OpenAIAdapter(api_key=api_key)
    if provider == "gemini":
        from providers.llm.gemini import GeminiAdapter

        return GeminiAdapter(api_key=api_key)
    if provider == "anthropic":
        from providers.llm.anthropic import AnthropicAdapter

        return AnthropicAdapter(api_key=api_key)
    from providers.llm.deepseek import DeepSeekAdapter

    return DeepSeekAdapter(api_key=api_key)


__all__ = ["FallbackAdapter", "LLMAdapter", "LLMResponse", "get_adapter"]
