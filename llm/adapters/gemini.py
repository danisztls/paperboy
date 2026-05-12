import logging

from .base import LLMAdapter

DEFAULT_MODEL = "gemini-2.0-flash"
log = logging.getLogger(__name__)


class GeminiAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "google-genai is required for the Gemini adapter: uv add google-genai"
            ) from None
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
    ) -> str | None:
        from google.genai import types
        _model = model or DEFAULT_MODEL
        config_kwargs: dict = {}
        if instructions:
            config_kwargs["system_instruction"] = instructions
        if web_search:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        try:
            response = await self._client.aio.models.generate_content(
                model=_model,
                contents=prompt,
                **({"config": config} if config else {}),
            )
            return (response.text or "").strip() or None
        except Exception as exc:
            log.error("Gemini completion failed: %s", exc)
            return None
