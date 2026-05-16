import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMAdapter, LLMResponse, timed_call

DEFAULT_MODEL = "claude-haiku-4-5"
TOOL_NAME = "structured_response"
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
        tool = {
            "name": TOOL_NAME,
            "description": f"Return a {response_model.__name__} object.",
            "input_schema": response_model.model_json_schema(),
        }
        kwargs: dict = {
            "model": _model,
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        if instructions:
            kwargs["system"] = instructions

        message, latency = await timed_call(
            log, "Anthropic", lambda: self._client.messages.create(**kwargs)
        )
        if message is None:
            return None
        tool_input: dict | None = None
        for block in message.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == TOOL_NAME
            ):
                tool_input = block.input
                break
        if tool_input is None:
            log.warning("Anthropic structured call returned no tool_use block")
            return None
        try:
            parsed = response_model.model_validate(tool_input)
        except ValidationError as exc:
            log.warning("Anthropic structured response failed validation: %s", exc)
            return None
        if trace is not None:
            trace["latency_s"] = latency
            trace["model_used"] = getattr(message, "model", _model)
            usage = getattr(message, "usage", None)
            if usage:
                trace["input_tokens"] = getattr(usage, "input_tokens", None)
                trace["output_tokens"] = getattr(usage, "output_tokens", None)
        return parsed
