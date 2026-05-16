import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_s: float
    reasoning: str | None = None
    finish_reason: str | None = None


async def timed_call[T](
    log: logging.Logger,
    provider: str,
    call: Callable[[], Awaitable[T]],
) -> tuple[T | None, float]:
    """Run an async provider call; return (result_or_None, elapsed_seconds).

    Logs and swallows exceptions so adapters don't repeat the try/except dance.
    """
    t0 = time.monotonic()
    try:
        return await call(), time.monotonic() - t0
    except Exception as exc:
        log.error("%s completion failed: %s", provider, repr(exc))
        return None, time.monotonic() - t0


def reasoning_level(reasoning: bool | str | dict) -> str | None:
    """Map the public reasoning value to a level string ('off'/'low'/'medium'/'high') or None.

    Adapters use this to pick provider-specific budgets/effort levels.
    - `False` / `"off"` / falsy → None (reasoning disabled)
    - `True` → "high" (back-compat with the old bool toggle)
    - "low" / "medium" / "high" → returned as-is
    - dict → "high" (caller's dict will be applied on top by the adapter)
    """
    if not reasoning or reasoning == "off":
        return None
    if reasoning is True or isinstance(reasoning, dict):
        return "high"
    if isinstance(reasoning, str):
        return reasoning
    return "high"


class LLMAdapter(ABC):
    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | str | dict = False,
    ) -> LLMResponse | None: ...

    @abstractmethod
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
        """Return a Pydantic instance via provider-native structured output.

        Returns None on provider failure or validation failure.
        """
        ...
