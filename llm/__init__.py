from llm.adapters.base import LLMAdapter


def get_adapter(provider: str | None = None, api_key: str | None = None) -> LLMAdapter:
    if provider == "gemini":
        from llm.adapters.gemini import GeminiAdapter
        return GeminiAdapter(api_key=api_key)
    from llm.adapters.openai import OpenAIAdapter
    return OpenAIAdapter(api_key=api_key)
