import logging
import time

from .base import LLMAdapter, LLMResponse

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
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        from google.genai import types

        _model = model or DEFAULT_MODEL
        config_kwargs: dict = {}
        if instructions:
            config_kwargs["system_instruction"] = instructions
        if web_search:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if reasoning:
            thinking_kwargs: dict = {"include_thoughts": True}
            if isinstance(reasoning, dict):
                thinking_kwargs.update(reasoning)
            config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_kwargs)
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        t0 = time.monotonic()
        try:
            response = await self._client.aio.models.generate_content(
                model=_model,
                contents=prompt,
                **({"config": config} if config else {}),
            )
        except Exception as exc:
            log.error("Gemini completion failed: %s", exc)
            return None
        latency = time.monotonic() - t0
        text = (response.text or "").strip()
        if not text:
            return None
        usage = getattr(response, "usage_metadata", None)
        reasoning_text: str | None = None
        if reasoning:
            thoughts: list[str] = []
            for cand in getattr(response, "candidates", []) or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", []) or []:
                    if getattr(part, "thought", False):
                        t = getattr(part, "text", "") or ""
                        if t:
                            thoughts.append(t)
            if thoughts:
                reasoning_text = "\n".join(thoughts).strip()
        return LLMResponse(
            text=text,
            model=_model,
            input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            latency_s=latency,
            reasoning=reasoning_text,
        )
