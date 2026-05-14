import logging

from providers.llm.base import LLMAdapter, LLMResponse

log = logging.getLogger(__name__)


class FallbackAdapter(LLMAdapter):
    """Tries each (adapter, model) entry in order, falling back on None return."""

    def __init__(self, entries: list[tuple[LLMAdapter, str | None]]) -> None:
        self._entries = entries

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        # If a per-task model override is provided, try it with the first adapter first,
        # then fall back to global entries starting from the second (haven't been tried yet).
        if model is not None:
            entries = [(self._entries[0][0], model), *self._entries[1:]]
        else:
            entries = self._entries
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
