import logging
import os
from typing import TYPE_CHECKING, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from .base import LLMAdapter, LLMResponse, timed_call

if TYPE_CHECKING:
    import instructor

DEFAULT_MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class DeepSeekAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._client = AsyncOpenAI(api_key=key, base_url=BASE_URL)
        self._instructor: instructor.AsyncInstructor | None = None

    def _get_instructor(self):
        if self._instructor is None:
            import instructor

            self._instructor = instructor.from_openai(self._client, mode=instructor.Mode.TOOLS)
        return self._instructor

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        _model = model or DEFAULT_MODEL
        if web_search:
            log.warning("DeepSeek adapter does not support web_search — ignoring")
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})
        thinking_cfg: dict = {"type": "enabled" if reasoning else "disabled"}
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
        # Thinking mode doesn't accept tool_choice="required" (what instructor.Mode.TOOLS sends);
        # disable it for structured calls regardless of the configured model.
        _model = model or DEFAULT_MODEL
        client = self._get_instructor()
        messages: list[dict] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})

        async def _call():
            return await client.chat.completions.create_with_completion(
                model=_model,
                messages=messages,
                response_model=response_model,
                max_retries=2,
                extra_body={"thinking": {"type": "disabled"}},
            )

        result, latency = await timed_call(log, "DeepSeek", _call)
        if result is None:
            return None
        obj, completion = result
        if trace is not None:
            trace["latency_s"] = latency
            trace["model_used"] = getattr(completion, "model", _model)
            usage = getattr(completion, "usage", None)
            if usage:
                trace["input_tokens"] = getattr(usage, "prompt_tokens", None)
                trace["output_tokens"] = getattr(usage, "completion_tokens", None)
        return obj
