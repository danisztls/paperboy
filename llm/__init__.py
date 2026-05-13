from llm.adapters.base import LLMAdapter


def get_adapter(provider: str | None = None, api_key: str | None = None) -> LLMAdapter:
    if provider == "gemini":
        from llm.adapters.gemini import GeminiAdapter

        return GeminiAdapter(api_key=api_key)
    if provider == "deepseek":
        from llm.adapters.deepseek import DeepSeekAdapter

        return DeepSeekAdapter(api_key=api_key)
    if provider == "anthropic":
        from llm.adapters.anthropic import AnthropicAdapter

        return AnthropicAdapter(api_key=api_key)
    from llm.adapters.openai import OpenAIAdapter

    return OpenAIAdapter(api_key=api_key)
