import logging
from typing import TYPE_CHECKING, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from .base import LLMAdapter, LLMResponse, timed_call

if TYPE_CHECKING:
    import instructor

DEFAULT_MODEL = "gpt-5.4-mini"
log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class OpenAIAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        self._client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
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
        tools: list[dict] = []
        if web_search:
            tool: dict = {"type": "web_search_preview"}
            if isinstance(web_search, dict):
                tool.update(web_search)
            tools = [tool]
        reasoning_arg: dict | None = None
        if reasoning:
            reasoning_arg = {"effort": "high", "summary": "auto"}
            if isinstance(reasoning, dict):
                reasoning_arg.update(reasoning)
        response, latency = await timed_call(
            log,
            "OpenAI",
            lambda: self._client.responses.create(
                model=_model,
                input=prompt,
                **({"instructions": instructions} if instructions else {}),
                **({"tools": tools} if tools else {}),
                **({"reasoning": reasoning_arg} if reasoning_arg else {}),
            ),
        )
        if response is None:
            return None
        text = (response.output_text or "").strip()
        if not text:
            return None
        usage = getattr(response, "usage", None)
        reasoning_text: str | None = None
        if reasoning_arg:
            for item in getattr(response, "output", []) or []:
                if getattr(item, "type", None) == "reasoning":
                    parts = [getattr(s, "text", "") for s in getattr(item, "summary", []) or []]
                    joined = "\n".join(p for p in parts if p).strip()
                    if joined:
                        reasoning_text = joined
                        break
        return LLMResponse(
            text=text,
            model=_model,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
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
            )

        result, latency = await timed_call(log, "OpenAI", _call)
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
