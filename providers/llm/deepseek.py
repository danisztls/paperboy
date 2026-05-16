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
        web_search: bool | dict = False,
        reasoning: bool | str | dict = False,
    ) -> LLMResponse | None:
        _model = model or DEFAULT_MODEL
        if web_search:
            log.warning("DeepSeek adapter does not support web_search — ignoring")
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})
        thinking_on = reasoning_level(reasoning) is not None
        thinking_cfg: dict = {"type": "enabled" if thinking_on else "disabled"}
        if isinstance(reasoning, dict):
            thinking_cfg.update(reasoning)
        response, latency = await timed_call(
            log,
            "DeepSeek",
            lambda: self._client.chat.completions.create(
                model=_model,
                messages=messages,
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
        return LLMResponse(
            text=text,
            model=_model,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            latency_s=latency,
            reasoning=reasoning_text,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
        )

    _structured_reasoning_warned = False

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
        if reasoning_level(reasoning) is not None and not type(self)._structured_reasoning_warned:
            log.warning(
                "DeepSeek thinking mode rejects tool_choice='required' / strict JSON, so "
                "reasoning is ignored on structured calls."
            )
            type(self)._structured_reasoning_warned = True
        _model = model or DEFAULT_MODEL
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        schema_note = f"\n\nRespond with JSON matching this schema:\n{schema}"
        sys_content = (instructions or "") + schema_note
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": prompt},
        ]
        response, latency = await timed_call(
            log,
            "DeepSeek",
            lambda: self._client.chat.completions.create(
                model=_model,
                messages=messages,
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
        return parsed
