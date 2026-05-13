import logging
import os
import time

from openai import AsyncOpenAI

from .base import LLMAdapter, LLMResponse

DEFAULT_MODEL = "deepseek-v4-flash"
REASONING_MODEL = "deepseek-reasoner"
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
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        _model = model or (REASONING_MODEL if reasoning else DEFAULT_MODEL)
        if web_search:
            log.warning("DeepSeek adapter does not support web_search — ignoring")
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})
        t0 = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=_model,
                messages=messages,
            )
        except Exception as exc:
            log.error("DeepSeek completion failed: %s", exc)
            return None
        latency = time.monotonic() - t0
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
