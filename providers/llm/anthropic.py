import logging
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from .base import LLMAdapter, LLMResponse, timed_call

if TYPE_CHECKING:
    import instructor

DEFAULT_MODEL = "claude-haiku-4-5"
log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class AnthropicAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError(
                "anthropic is required for the Anthropic adapter: uv add anthropic"
            ) from None
        self._client = (
            _anthropic.AsyncAnthropic(api_key=api_key) if api_key else _anthropic.AsyncAnthropic()
        )
        self._instructor: instructor.AsyncInstructor | None = None

    def _get_instructor(self):
        if self._instructor is None:
            import instructor

            self._instructor = instructor.from_anthropic(self._client)
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
        tools: list[dict] = []
        if web_search:
            tools = [{"type": "web_search_20260209", "name": "web_search"}]
        kwargs: dict = {}
        if instructions:
            kwargs["system"] = instructions
        if tools:
            kwargs["tools"] = tools
        if reasoning:
            thinking_arg: dict = {"type": "enabled", "budget_tokens": 8000}
            if isinstance(reasoning, dict):
                thinking_arg.update(reasoning)
            kwargs["thinking"] = thinking_arg

        async def _call():
            async with self._client.messages.stream(
                model=_model,
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            ) as stream:
                return await stream.get_final_message()

        message, latency = await timed_call(log, "Anthropic", _call)
        if message is None:
            return None
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            return None
        reasoning_text: str | None = None
        if reasoning:
            thoughts = [
                getattr(block, "thinking", "")
                for block in message.content
                if block.type == "thinking"
            ]
            joined = "\n".join(t for t in thoughts if t).strip()
            if joined:
                reasoning_text = joined
        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=text,
            model=_model,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            latency_s=latency,
            reasoning=reasoning_text,
            finish_reason=getattr(message, "stop_reason", None),
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
        _model = model or DEFAULT_MODEL
        client = self._get_instructor()
        kwargs: dict = {
            "model": _model,
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": prompt}],
            "response_model": response_model,
            "max_retries": 2,
        }
        if instructions:
            kwargs["system"] = instructions

        async def _call():
            return await client.messages.create_with_completion(**kwargs)

        result, latency = await timed_call(log, "Anthropic", _call)
        if result is None:
            return None
        obj, message = result
        if trace is not None:
            trace["latency_s"] = latency
            trace["model_used"] = getattr(message, "model", _model)
            usage = getattr(message, "usage", None)
            if usage:
                trace["input_tokens"] = getattr(usage, "input_tokens", None)
                trace["output_tokens"] = getattr(usage, "output_tokens", None)
        return obj
