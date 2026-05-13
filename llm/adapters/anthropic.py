import logging

from .base import LLMAdapter

DEFAULT_MODEL = "claude-haiku-4-5"
log = logging.getLogger(__name__)


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
    ) -> str | None:
        _model = model or DEFAULT_MODEL
        tools: list[dict] = []
        if web_search:
            tools = [{"type": "web_search_20260209", "name": "web_search"}]
        kwargs: dict = {}
        if instructions:
            kwargs["system"] = instructions
        if tools:
            kwargs["tools"] = tools
        try:
            async with self._client.messages.stream(
                model=_model,
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            ) as stream:
                message = await stream.get_final_message()
            text = "".join(block.text for block in message.content if block.type == "text")
            return text.strip() or None
        except Exception as exc:
            log.error("Anthropic completion failed: %s", exc)
            return None
