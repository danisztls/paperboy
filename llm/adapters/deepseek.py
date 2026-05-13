import logging
import os

from openai import AsyncOpenAI

from .base import LLMAdapter

DEFAULT_MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com"
log = logging.getLogger(__name__)


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
    ) -> str | None:
        _model = model or DEFAULT_MODEL
        if web_search:
            log.warning("DeepSeek adapter does not support web_search — ignoring")
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})
        try:
            response = await self._client.chat.completions.create(
                model=_model,
                messages=messages,
            )
            text = (response.choices[0].message.content or "").strip()
            return text or None
        except Exception as exc:
            log.error("DeepSeek completion failed: %s", exc)
            return None
