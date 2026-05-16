import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMAdapter, LLMResponse, timed_call

DEFAULT_MODEL = "gemini-2.0-flash"
log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


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
        response, latency = await timed_call(
            log,
            "Gemini",
            lambda: self._client.aio.models.generate_content(
                model=_model,
                contents=prompt,
                **({"config": config} if config else {}),
            ),
        )
        if response is None:
            return None
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
        from google.genai import types

        _model = model or DEFAULT_MODEL
        config_kwargs: dict = {
            "response_mime_type": "application/json",
            "response_schema": response_model,
        }
        if instructions:
            config_kwargs["system_instruction"] = instructions
        config = types.GenerateContentConfig(**config_kwargs)
        response, latency = await timed_call(
            log,
            "Gemini",
            lambda: self._client.aio.models.generate_content(
                model=_model,
                contents=prompt,
                config=config,
            ),
        )
        if response is None:
            return None
        parsed = getattr(response, "parsed", None)
        if parsed is None:
            try:
                parsed = response_model.model_validate_json(response.text or "")
            except ValidationError as exc:
                log.warning("Gemini structured response failed validation: %s", exc)
                return None
        if trace is not None:
            trace["latency_s"] = latency
            trace["model_used"] = getattr(response, "model_version", _model)
            usage = getattr(response, "usage_metadata", None)
            if usage:
                trace["input_tokens"] = getattr(usage, "prompt_token_count", None)
                trace["output_tokens"] = getattr(usage, "candidates_token_count", None)
        return parsed
