from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_s: float
    reasoning: str | None = None
    finish_reason: str | None = None


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
