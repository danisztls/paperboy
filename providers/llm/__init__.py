import logging
from typing import TypeVar

from pydantic import BaseModel

from providers.llm.base import LLMAdapter, LLMResponse

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class FallbackAdapter(LLMAdapter):
    """Tries each (adapter, model) entry in order, falling back on None return."""

    def __init__(self, entries: list[tuple[LLMAdapter, str | None]]) -> None:
        self._entries = entries

    def _effective_entries(self, model: str | None) -> list[tuple[LLMAdapter, str | None]]:
        if model is not None:
            return [(self._entries[0][0], model), *self._entries[1:]]
        return self._entries

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        entries = self._effective_entries(model)
        for i, (adapter, effective_model) in enumerate(entries):
            result = await adapter.complete(
                prompt,
                model=effective_model,
                instructions=instructions,
                web_search=web_search,
                reasoning=reasoning,
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
        reasoning: bool | dict = False,
        trace: dict | None = None,
    ) -> T | None:
        entries = self._effective_entries(model)
        for i, (adapter, effective_model) in enumerate(entries):
            result = await adapter.complete_structured(
                prompt,
                response_model,
                model=effective_model,
                instructions=instructions,
                reasoning=reasoning,
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
