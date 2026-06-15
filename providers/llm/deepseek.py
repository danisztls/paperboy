import json
import logging
import os
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .base import LLMAdapter, LLMResponse, reasoning_level, timed_call

DEFAULT_MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _cache_tokens(usage) -> tuple[int | None, int | None]:
    """Pull DeepSeek's prompt-cache hit/miss counts off the usage object.

    DeepSeek returns ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
    as fields the OpenAI SDK doesn't declare, so they land in ``model_extra``.
    """
    if usage is None:
        return None, None
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if hit is None and miss is None:
        extra = getattr(usage, "model_extra", None) or {}
        hit = extra.get("prompt_cache_hit_tokens")
        miss = extra.get("prompt_cache_miss_tokens")
    return hit, miss


class DeepSeekAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._client = AsyncOpenAI(api_key=key, base_url=BASE_URL)

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        messages: list[dict] | None = None,
        reasoning: bool | str | dict = False,
    ) -> LLMResponse | None:
        _model = model or DEFAULT_MODEL
        if messages is not None:
            req_messages = messages
        else:
            req_messages = []
            if instructions:
                req_messages.append({"role": "system", "content": instructions})
            req_messages.append({"role": "user", "content": prompt})
        thinking_on = reasoning_level(reasoning) is not None
        thinking_cfg: dict = {"type": "enabled" if thinking_on else "disabled"}
        if isinstance(reasoning, dict):
            thinking_cfg.update(reasoning)
        response, latency = await timed_call(
            log,
            "DeepSeek",
            lambda: self._client.chat.completions.create(
                model=_model,
                messages=req_messages,
                extra_body={"thinking": thinking_cfg},
            ),
        )
        if response is None:
            return None
        message = response.choices[0].message
        text = (message.content or "").strip()
        if not text:
            return None
        reasoning_text = (getattr(message, "reasoning_content", None) or "").strip() or None
        usage = getattr(response, "usage", None)
        cache_hit, cache_miss = _cache_tokens(usage)
        return LLMResponse(
            text=text,
            model=_model,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            latency_s=latency,
            reasoning=reasoning_text,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
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
        messages: list[dict] | None = None,
        reasoning: bool | str | dict = False,
        trace: dict | None = None,
    ) -> T | None:
        _model = model or DEFAULT_MODEL
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        schema_note = f"\n\nRespond with JSON matching this schema:\n{schema}"

        if reasoning_level(reasoning) is not None:
            # Thinking mode is incompatible with json_object / tool_choice on
            # DeepSeek, so delegate to complete() which supports it natively.
            if messages is not None:
                # Keep the caller's conversation as a cache-stable prefix; append
                # the schema ask as a trailing turn so the prefix stays identical.
                convo = list(messages) + [{"role": "user", "content": schema_note}]
                resp = await self.complete("", messages=convo, model=_model, reasoning=reasoning)
            else:
                resp = await self.complete(
                    prompt,
                    model=_model,
                    instructions=(instructions or "") + schema_note,
                    reasoning=reasoning,
                )
            if resp is None:
                return None
            try:
                parsed = response_model.model_validate_json(resp.text)
            except ValidationError as exc:
                log.warning("DeepSeek structured response failed validation: %s", exc)
                return None
            if trace is not None:
                trace["latency_s"] = resp.latency_s
                trace["model_used"] = resp.model
                trace["input_tokens"] = resp.input_tokens
                trace["output_tokens"] = resp.output_tokens
                trace["reasoning"] = resp.reasoning
                trace["cache_hit_tokens"] = resp.cache_hit_tokens
                trace["cache_miss_tokens"] = resp.cache_miss_tokens
            return parsed

        if messages is not None:
            req_messages = list(messages) + [{"role": "user", "content": schema_note}]
        else:
            req_messages = [
                {"role": "system", "content": (instructions or "") + schema_note},
                {"role": "user", "content": prompt},
            ]
        response, latency = await timed_call(
            log,
            "DeepSeek",
            lambda: self._client.chat.completions.create(
                model=_model,
                messages=req_messages,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            ),
        )
        if response is None:
            return None
        text = response.choices[0].message.content or ""
        try:
            parsed = response_model.model_validate_json(text)
        except ValidationError as exc:
            log.warning("DeepSeek structured response failed validation: %s", exc)
            return None
        if trace is not None:
            trace["latency_s"] = latency
            trace["model_used"] = getattr(response, "model", _model)
            usage = getattr(response, "usage", None)
            if usage:
                trace["input_tokens"] = getattr(usage, "prompt_tokens", None)
                trace["output_tokens"] = getattr(usage, "completion_tokens", None)
                hit, miss = _cache_tokens(usage)
                trace["cache_hit_tokens"] = hit
                trace["cache_miss_tokens"] = miss
        return parsed
