"""Shared fixtures for pipeline e2e tests.

Tests substitute fakes at two boundaries only:
- The LLM adapter (a parameter on _process_*_task), via FakeLLMAdapter.
- The aiohttp transport, via aioresponses.

Real RSSSource, Discord*Target, File*Target run unchanged.
"""

import json
import pathlib

import pytest
from aioresponses import aioresponses

from providers.llm.base import LLMAdapter, LLMResponse

WEBHOOK_URL = "https://discord.example/webhook"
_FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


class FakeLLMAdapter(LLMAdapter):
    """LLMAdapter whose complete() pops from a pre-seeded queue.

    Use queue() to enqueue an LLMResponse (or None to simulate provider failure).
    Calls beyond the queue raise loudly so missing fixtures fail tests early.
    """

    def __init__(self) -> None:
        self._responses: list[LLMResponse | None] = []
        self.calls: list[dict] = []

    def queue(self, response: LLMResponse | None) -> None:
        self._responses.append(response)

    def queue_text(self, text: str) -> None:
        self.queue(make_response(text))

    def queue_filter(
        self,
        items: list[dict],
        memory: str = "",
    ) -> None:
        """Convenience: queue a filter-shaped JSON response."""
        payload = {"items": items, "memory": memory}
        self.queue_text(json.dumps(payload))

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        web_search: bool | dict = False,
        reasoning: bool | dict = False,
    ) -> LLMResponse | None:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "instructions": instructions,
                "web_search": web_search,
                "reasoning": reasoning,
            }
        )
        if not self._responses:
            raise AssertionError(
                f"FakeLLMAdapter exhausted — no canned response for call #{len(self.calls)}"
            )
        return self._responses.pop(0)


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
        "image": {"skip": True},
    }
    if llm_filter is not None:
        cfg["llm"] = llm_filter
    if kind is not None:
        cfg["kind"] = kind
    if summarize is not None:
        cfg["summarize"] = summarize
    return cfg


def make_search_cfg(
    *,
    name: str = "test-search",
    prompt: str = "tell me something",
    file_path: str | None = None,
) -> dict:
    push: list[dict] = [{"discord": {"webhook": WEBHOOK_URL}}]
    if file_path is not None:
        push.append({"file": file_path})
    return {
        "name": name,
        "pull": [{"llm": {"prompt": prompt, "web_search": True}}],
        "push": push,
    }
