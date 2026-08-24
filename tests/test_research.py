# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests for the agentic research loop (pull/research.py) and its task wrapper.

The two fakes: FakeLLMAdapter (scripted complete_structured decisions + complete
synthesis) and FakeVasco (records search/extract calls, replays canned results).
No network, no real LLM, no vascod.
"""

import aiohttp
import pytest

from pull.research import ResearchAction, _linkify_citations, run_research_task
from tasks import process_research_task
from tests.conftest import make_ctx, make_research_cfg

WEBHOOK_URL = "https://discord.example/webhook"


def test_linkify_citations():
    urls = {1: "https://a", 2: "https://b"}
    # single, adjacent, and comma-grouped markers all become digest-style [[n](url)] links
    assert (
        _linkify_citations("see [1] and [2]", urls) == "see [[1](https://a)] and [[2](https://b)]"
    )
    assert _linkify_citations("x [1][2]", urls) == "x [[1](https://a)][[2](https://b)]"
    assert _linkify_citations("both [1, 2]", urls) == "both [[1](https://a)] [[2](https://b)]"
    # a number with no known URL is left untouched (never breaks)
    assert _linkify_citations("ref [3]", urls) == "ref [3]"


class FakeVasco:
    """Stand-in for process._vasco: records calls, replays canned results."""

    def __init__(self, *, search_results=None, passages=None):
        self.search_results = search_results or {}  # query -> list[dict] | None
        self.passages = passages or {}  # url -> list[dict] | None
        self.searched: list[str] = []
        self.read: list[tuple[str, str]] = []

    async def search(self, query, *, max_results=10, region=None, site=None):
        self.searched.append(query)
        return self.search_results.get(query)

    async def extract(self, url, query, *, top=5):
        self.read.append((url, query))
        return self.passages.get(url)


@pytest.fixture
def patch_vasco(monkeypatch):
    """Install a FakeVasco over process._vasco.search/extract (same module object
    that pull.research holds a reference to)."""

    def _install(fake: FakeVasco) -> FakeVasco:
        monkeypatch.setattr("process._vasco.search", fake.search)
        monkeypatch.setattr("process._vasco.extract", fake.extract)
        return fake

    return _install


async def test_research_loop_happy(fake_adapter, patch_vasco):
    """search → read two URLs → finish → synthesize over the gathered passages."""
    fake = patch_vasco(
        FakeVasco(
            search_results={
                "q1": [
                    {"title": "T1", "url": "https://u1", "snippet": "s1"},
                    {"title": "T2", "url": "https://u2", "snippet": "s2"},
                ]
            },
            passages={
                "https://u1": [{"text": "passage one about the topic"}],
                "https://u2": [{"text": "passage two about the topic"}],
            },
        )
    )
    fake_adapter.queue_structured(ResearchAction(kind="search", queries=["q1"], rationale="find"))
    fake_adapter.queue_structured(
        ResearchAction(kind="read", urls=["https://u1", "https://u2"], rationale="read")
    )
    fake_adapter.queue_structured(ResearchAction(kind="finish", rationale="enough"))
    fake_adapter.queue_text("FINAL ANSWER [1][2]")

    trace: dict = {}
    answer = await run_research_task(
        make_research_cfg(prompt="explain the topic"), adapter=fake_adapter, trace=trace
    )

    # Inline citations are linkified to digest-style [[n](url)]; the <...> embed
    # suppression is added later by push.discord.post_text_to_discord, not here.
    assert answer == "FINAL ANSWER [[1](https://u1)][[2](https://u2)]"
    assert fake.searched == ["q1"]
    assert set(fake.read) == {
        ("https://u1", "explain the topic"),
        ("https://u2", "explain the topic"),
    }
    assert len(trace["steps"]) == 3
    assert {s["url"] for s in trace["sources"]} == {"https://u1", "https://u2"}
    # The synthesis prompt (last complete() call) carried the extracted passages.
    synthesis_prompt = fake_adapter.calls[-1]["prompt"]
    assert "passage one about the topic" in synthesis_prompt
    assert "passage two about the topic" in synthesis_prompt


async def test_research_dedups_queries_and_urls(fake_adapter, patch_vasco):
    """Repeated queries / already-read URLs are not re-issued."""
    fake = patch_vasco(
        FakeVasco(
            search_results={"q1": [{"title": "T", "url": "https://u", "snippet": "s"}]},
            passages={"https://u": [{"text": "p"}]},
        )
    )
    fake_adapter.queue_structured(
        ResearchAction(kind="search", queries=["q1", "q1"], rationale="x")
    )
    fake_adapter.queue_structured(
        ResearchAction(kind="read", urls=["https://u", "https://u"], rationale="x")
    )
    fake_adapter.queue_structured(ResearchAction(kind="finish", rationale="done"))
    fake_adapter.queue_text("ok")

    await run_research_task(make_research_cfg(prompt="x"), adapter=fake_adapter)

    assert fake.searched == ["q1"]  # duplicate query collapsed
    assert fake.read == [("https://u", "x")]  # duplicate read collapsed


async def test_research_respects_max_steps(fake_adapter, patch_vasco):
    """The loop stops after max_steps decisions even if the LLM never finishes."""
    fake = patch_vasco(FakeVasco(search_results={f"q{i}": [] for i in range(5)}))
    for i in range(5):
        fake_adapter.queue_structured(
            ResearchAction(kind="search", queries=[f"q{i}"], rationale="more")
        )
    fake_adapter.queue_text("answer from limited steps")

    answer = await run_research_task(
        make_research_cfg(prompt="x", max_steps=2), adapter=fake_adapter
    )

    assert answer == "answer from limited steps"
    assert len(fake_adapter.structured_calls) == 2  # only 2 decisions consumed
    assert fake.searched == ["q0", "q1"]


async def test_research_respects_max_reads(fake_adapter, patch_vasco):
    """max_reads caps how many URLs are extracted in a single read action."""
    urls = [f"https://u{i}" for i in range(5)]
    fake = patch_vasco(
        FakeVasco(
            search_results={"q": [{"title": "T", "url": u, "snippet": "s"} for u in urls]},
            passages={u: [{"text": f"p{u}"}] for u in urls},
        )
    )
    fake_adapter.queue_structured(ResearchAction(kind="search", queries=["q"], rationale="x"))
    fake_adapter.queue_structured(ResearchAction(kind="read", urls=urls, rationale="x"))
    fake_adapter.queue_structured(ResearchAction(kind="finish", rationale="done"))
    fake_adapter.queue_text("answer")

    await run_research_task(make_research_cfg(prompt="x", max_reads=2), adapter=fake_adapter)

    assert len(fake.read) == 2  # a single read action is capped at max_reads


async def test_research_handles_vascod_failure(fake_adapter, patch_vasco):
    """A vascod None (unreachable/failure) is a no-op, not a crash."""
    fake = patch_vasco(FakeVasco(search_results={}))  # search returns None for any query
    fake_adapter.queue_structured(ResearchAction(kind="search", queries=["q1"], rationale="find"))
    fake_adapter.queue_structured(ResearchAction(kind="finish", rationale="give up"))
    fake_adapter.queue_text("could not find sources")

    answer = await run_research_task(make_research_cfg(prompt="x"), adapter=fake_adapter)

    assert answer == "could not find sources"
    assert fake.searched == ["q1"]


async def test_research_stops_when_decision_is_none(fake_adapter, patch_vasco):
    """A provider failure on a decision (None) ends the loop; we still synthesize."""
    patch_vasco(FakeVasco())
    fake_adapter.queue_structured(None)  # provider failure on the very first decision
    fake_adapter.queue_text("answer from nothing")

    answer = await run_research_task(make_research_cfg(prompt="x"), adapter=fake_adapter)

    assert answer == "answer from nothing"
    assert len(fake_adapter.structured_calls) == 1


async def test_pipeline_research_happy(mock_http, fake_adapter, patch_vasco, tmp_path):
    """End-to-end: process_research_task posts the answer to Discord + a file."""
    mock_http.post(WEBHOOK_URL, status=204)
    patch_vasco(
        FakeVasco(
            search_results={"q1": [{"title": "T1", "url": "https://u1", "snippet": "s1"}]},
            passages={"https://u1": [{"text": "the answer content"}]},
        )
    )
    fake_adapter.queue_structured(ResearchAction(kind="search", queries=["q1"], rationale="find"))
    fake_adapter.queue_structured(
        ResearchAction(kind="read", urls=["https://u1"], rationale="read")
    )
    fake_adapter.queue_structured(ResearchAction(kind="finish", rationale="done"))
    fake_adapter.queue_text("hello world answer")

    out_file = tmp_path / "out.md"
    cfg = make_research_cfg(file_path=str(out_file))
    async with aiohttp.ClientSession() as session:
        result = await process_research_task(
            cfg, {"tasks": {}}, make_ctx(session, research=fake_adapter)
        )

    assert result == {"test-research": {"last_run": result["test-research"]["last_run"]}}
    assert "hello world answer" in out_file.read_text()
    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 1
