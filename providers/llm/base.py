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
        log.error("%s completion failed: %s", provider, exc)
        return None, time.monotonic() - t0


class LLMAdapter(ABC):
    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | dict = False,
    ) -> LLMResponse | None: ...

    @abstractmethod
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
        """Return a Pydantic instance via provider-native structured output.

        Returns None on provider failure or unrecoverable validation failure.
        The provider library (e.g. instructor) handles the parse + retry loop.
        """
        ...
