import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMAdapter, LLMResponse, reasoning_level, timed_call

DEFAULT_MODEL = "gemini-2.5-flash"
log = logging.getLogger(__name__)

# Thinking budget per effort level. Provider-specific; tune as new models land.
_THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}

T = TypeVar("T", bound=BaseModel)


def _thinking_config(reasoning: bool | str | dict):
    from google.genai import types

    level = reasoning_level(reasoning)
    if level is None:
        return None
    kwargs: dict = {
        "include_thoughts": True,
        "thinking_budget": _THINKING_BUDGETS.get(level, _THINKING_BUDGETS["high"]),
    }
    if isinstance(reasoning, dict):
        kwargs.update(reasoning)
    return types.ThinkingConfig(**kwargs)


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
        messages: list[dict] | None = None,
        reasoning: bool | str | dict = False,
    ) -> LLMResponse | None:
        from google.genai import types

        _model = model or DEFAULT_MODEL
        config_kwargs: dict = {}
        if messages is not None:
            # Gemini has no "system" content role: system turns fold into
            # system_instruction, the rest map to contents (assistant → model).
            sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
            sys_text = "\n\n".join(sys_parts) if sys_parts else instructions
            contents = [
                {
                    "role": "model" if m.get("role") == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in messages
                if m.get("role") != "system"
            ]
        else:
            sys_text = instructions
            contents = prompt
        if sys_text:
            config_kwargs["system_instruction"] = sys_text
        thinking_cfg = _thinking_config(reasoning)
        if thinking_cfg is not None:
            config_kwargs["thinking_config"] = thinking_cfg
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        response, latency = await timed_call(
            log,
            "Gemini",
            lambda: self._client.aio.models.generate_content(
                model=_model,
                contents=contents,
                **({"config": config} if config else {}),
            ),
        )
        if response is None:
            return None
        text = (response.text or "").strip()
        if not text:
            return None
        usage = getattr(response, "usage_metadata", None)
        cache_hit = getattr(usage, "cached_content_token_count", None) if usage else None
        prompt_tok = getattr(usage, "prompt_token_count", None) if usage else None
        cache_miss = (
            prompt_tok - cache_hit if (prompt_tok is not None and cache_hit is not None) else None
        )
        reasoning_text: str | None = None
        if thinking_cfg is not None:
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
            input_tokens=prompt_tok,
            output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            latency_s=latency,
            reasoning=reasoning_text,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
        )

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
        from google.genai import types

        _model = model or DEFAULT_MODEL
        config_kwargs: dict = {
            "response_mime_type": "application/json",
            "response_schema": response_model,
        }
        if instructions:
            config_kwargs["system_instruction"] = instructions
        thinking_cfg = _thinking_config(reasoning)
        if thinking_cfg is not None:
            config_kwargs["thinking_config"] = thinking_cfg
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
