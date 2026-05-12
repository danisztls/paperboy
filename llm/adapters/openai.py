import logging

from openai import AsyncOpenAI

from .base import LLMAdapter

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
    ) -> str | None:
        _model = model or DEFAULT_MODEL
        tools: list[dict] = []
        if web_search:
            tool: dict = {"type": "web_search_preview"}
            if isinstance(web_search, dict):
                tool.update(web_search)
            tools = [tool]
        try:
            response = await self._client.responses.create(
                model=_model,
                input=prompt,
                **({"instructions": instructions} if instructions else {}),
                **({"tools": tools} if tools else {}),
            )
            return (response.output_text or "").strip() or None
        except Exception as exc:
            log.error("OpenAI completion failed: %s", exc)
            return None
