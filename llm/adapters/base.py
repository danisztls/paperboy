from abc import ABC, abstractmethod


class LLMAdapter(ABC):
    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
    ) -> str | None: ...
