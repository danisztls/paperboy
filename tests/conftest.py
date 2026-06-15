"""Shared fixtures for pipeline e2e tests.

Tests substitute fakes at two boundaries only:
- The LLM adapter (a parameter on _process_*_task), via FakeLLMAdapter.
- The aiohttp transport, via aioresponses.

Real RSSSource, Discord*Target, File*Target run unchanged.
"""

import pathlib
from typing import TypeVar

import pytest
from aioresponses import aioresponses
from pydantic import BaseModel

from process.curate import CurateParagraph, FilterDecisions, FilterItem
from providers.llm.base import LLMAdapter, LLMResponse, ModelHandle
from tasks import LLMHandles, RunContext

WEBHOOK_URL = "https://discord.example/webhook"
_FIXTURES = pathlib.Path(__file__).parent / "fixtures"

T = TypeVar("T", bound=BaseModel)


def make_ctx(
    session,
    *,
    curate: LLMAdapter | None = None,
    summarize: LLMAdapter | None = None,
    research: LLMAdapter | None = None,
    curate_reasoning=None,
    config: dict | None = None,
    analysis: bool = False,
    collector=None,
) -> RunContext:
    """Build a RunContext for direct process_*_task calls, wrapping fake adapters."""

    def _handle(adapter, reasoning=None):
        return ModelHandle(adapter, reasoning=reasoning) if adapter is not None else None

    return RunContext(
        session=session,
        config=config or {},
        llm=LLMHandles(
            curate=_handle(curate, curate_reasoning),
            summarize=_handle(summarize),
            research=_handle(research),
        ),
        collector=collector,
        analysis=analysis,
    )


def load_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


class FakeLLMAdapter(LLMAdapter):
    """LLMAdapter whose complete() / complete_structured() pop from pre-seeded queues.

    Use queue() to enqueue an LLMResponse for free-form completions (None simulates
    provider failure). Use queue_structured() / queue_filter() for Pydantic returns.
    Calls beyond the queue raise loudly so missing fixtures fail tests early.
    """

    def __init__(self) -> None:
        self._responses: list[LLMResponse | None] = []
        self._structured: list[BaseModel | None] = []
        self.calls: list[dict] = []
        self.structured_calls: list[dict] = []

    def queue(self, response: LLMResponse | None) -> None:
        self._responses.append(response)

    def queue_text(self, text: str) -> None:
        self.queue(make_response(text))

    def queue_structured(self, value: BaseModel | None) -> None:
        self._structured.append(value)

    def queue_filter(
        self,
        items: list[dict],
        memory: list[dict] | None = None,
    ) -> None:
        """Convenience: queue a FilterDecisions for the next complete_structured call.

        memory is a list of {"text": str, "citations": list[int]} dicts.
        """
        paragraphs = [
            CurateParagraph(
                text=p["text"],
                citations=p.get("citations", []),
                section=p.get("section"),
            )
            for p in (memory or [])
        ]
        self.queue_structured(
            FilterDecisions(
                items=[
                    FilterItem(id=it["id"], passes=it["pass"], reason=it["reason"]) for it in items
                ],
                memory=paragraphs,
            )
        )

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        messages: list[dict] | None = None,
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "instructions": instructions,
                "messages": messages,
                "reasoning": reasoning,
            }
        )
        if not self._responses:
            raise AssertionError(
                f"FakeLLMAdapter exhausted — no canned response for call #{len(self.calls)}"
            )
        return self._responses.pop(0)

    async def complete_structured(
        self,
        prompt: str,
        response_model: type[T],
        *,
        model: str | None = None,
        instructions: str | None = None,
        messages: list[dict] | None = None,
        reasoning: bool | dict = False,
        trace: dict | None = None,
    ) -> T | None:
        self.structured_calls.append(
            {
                "prompt": prompt,
                "response_model": response_model,
                "model": model,
                "instructions": instructions,
                "messages": messages,
                "reasoning": reasoning,
            }
        )
        if not self._structured:
            raise AssertionError(
                f"FakeLLMAdapter exhausted — no structured response for call #{len(self.structured_calls)}"
            )
        return self._structured.pop(0)


def make_response(text: str, *, model: str = "fake-model") -> LLMResponse:
    return LLMResponse(
        text=text,
        model=model,
        input_tokens=10,
        output_tokens=20,
        latency_s=0.01,
    )


@pytest.fixture
def fake_adapter() -> FakeLLMAdapter:
    return FakeLLMAdapter()


@pytest.fixture
def mock_http():
    with aioresponses() as m:
        yield m


def make_curate_cfg(
    *,
    name: str = "test-curate",
    feeds: list[dict] | None = None,
    file_path: str | None = None,
    llm_filter: dict | None = None,
    kind: str | None = None,
    summarize: bool | None = None,
) -> dict:
    """Build a minimal task_cfg dict for a curate/digest task."""
    if feeds is None:
        feeds = [{"url": "https://feed.example/rss", "name": "Example"}]
    push: list[dict] = [{"discord": {"webhook": WEBHOOK_URL}}]
    if file_path is not None:
        push.append({"file": file_path})
    cfg: dict = {
        "name": name,
        "pull": [{"feed": f} for f in feeds],
        "push": push,
        "ignore": {"image": True},
    }
    if llm_filter is not None:
        cfg["curate"] = llm_filter
    if kind is not None:
        cfg["kind"] = kind
    if summarize is not None:
        cfg["summarize"] = summarize
    return cfg


def make_research_cfg(
    *,
    name: str = "test-research",
    prompt: str = "tell me something",
    file_path: str | None = None,
    **research_kwargs,
) -> dict:
    push: list[dict] = [{"discord": {"webhook": WEBHOOK_URL}}]
    if file_path is not None:
        push.append({"file": file_path})
    return {
        "name": name,
        "pull": [{"research": {"prompt": prompt, **research_kwargs}}],
        "push": push,
    }
