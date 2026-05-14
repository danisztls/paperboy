import logging

from openai import AsyncOpenAI

from .base import LLMAdapter, LLMResponse, timed_call

DEFAULT_MODEL = "gpt-5.4-mini"
log = logging.getLogger(__name__)


class OpenAIAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        self._client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()

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
