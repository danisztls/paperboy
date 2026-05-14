"""E2E tests for the LLM curate (RSS) pipeline.

Real RSSSource, Discord*Target, FileEmbedTarget run. Only the LLM adapter
(passed as a parameter to _process_llm_curate_task) and the aiohttp transport
(via aioresponses) are faked.
"""

import aiohttp

from tasks import _process_llm_curate_task
from tests.conftest import load_fixture, make_curate_cfg

FEED_URL = "https://feed.example/rss"
FEED_B_URL = "https://b.example.com/rss"
WEBHOOK_URL = "https://discord.example/webhook"


async def test_curate_happy(mock_http, fake_adapter, tmp_path):
    """Happy path: 2 items in, filter passes 1, drops 1. Real Discord + file push."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    mock_http.post(WEBHOOK_URL, status=204)

    # get_new_entries reverses feedparser's order (oldest-first), so id=0 is
    # the item that appears second in the XML (quantum), id=1 is the first (cats).
    fake_adapter.queue_filter(
        items=[
            {"id": 0, "pass": False, "reason": "Off-topic."},
            {"id": 1, "pass": True, "reason": "Cats are great."},
        ],
        memory="Cat news today [1].",
    )

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"prompt": "pass items about cats"},
    )

    async with aiohttp.ClientSession() as session:
        result = await _process_llm_curate_task(
            cfg, {"tasks": {}}, session, llm_adapter=fake_adapter
        )

    assert "test-curate" in result
    feeds_state = result["test-curate"]["feeds"]
    assert FEED_URL in feeds_state
    items = feeds_state[FEED_URL]["items"]
    assert len(items) == 2
    by_url = {it["url"]: it for it in items}
    assert by_url["https://example.com/posts/1"]["filter_pass"] is True
    assert by_url["https://example.com/posts/2"]["filter_pass"] is False
    assert feeds_state[FEED_URL]["last_run"]

    body = out_file.read_text()
    assert "First Item About Cats" in body
    assert "Second Item About Quantum Physics" not in body

    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 1, f"expected 1 Discord POST, got {len(posts)}"

    memory_log = result["test-curate"]["memory"]
    assert len(memory_log) == 1
    entry = next(iter(memory_log.values()))
    assert "Cat news today" in entry
    assert "[0]" not in entry  # cite markers stripped per _CITE_STRIP_RE


async def test_curate_dedup(mock_http, fake_adapter, tmp_path):
    """Items already in state are not re-pushed and not sent to the filter."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    mock_http.post(WEBHOOK_URL, status=204)

    fake_adapter.queue_filter(
        items=[{"id": 0, "pass": True, "reason": "Quantum is on-topic now."}],
        memory="Quantum news [0].",
    )

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"prompt": "pass anything"},
    )
    state = {
        "tasks": {
            "test-curate": {
                "feeds": {
                    FEED_URL: {
                        "items": [
                            {
                                "url": "https://example.com/posts/1",
                                "title": "First Item About Cats",
                                "access_date": "2026-05-01T00:00:00+00:00",
                            }
                        ],
                        "last_run": "2026-05-01T00:00:00+00:00",
                    }
                }
            }
        }
    }

    async with aiohttp.ClientSession() as session:
        result = await _process_llm_curate_task(cfg, state, session, llm_adapter=fake_adapter)

    body = out_file.read_text()
    assert "Second Item About Quantum Physics" in body
    assert "First Item About Cats" not in body

    items = result["test-curate"]["feeds"][FEED_URL]["items"]
    urls = {it["url"] for it in items}
    assert urls == {
        "https://example.com/posts/1",
        "https://example.com/posts/2",
    }


async def test_curate_pull_failure(mock_http, fake_adapter, tmp_path):
    """One feed returning HTTP error must NOT update its state; the other proceeds."""
    mock_http.get(FEED_URL, status=500)
    mock_http.get(FEED_B_URL, body=load_fixture("feed_b.xml"))
    mock_http.post(WEBHOOK_URL, status=204)

    fake_adapter.queue_filter(
        items=[{"id": 0, "pass": True, "reason": "All gardening passes."}],
        memory="Garden tips [0].",
    )

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[
            {"url": FEED_URL, "name": "Example"},
            {"url": FEED_B_URL, "name": "Feed B"},
        ],
        file_path=str(out_file),
        llm_filter={"prompt": "pass anything"},
    )

    async with aiohttp.ClientSession() as session:
        result = await _process_llm_curate_task(
            cfg, {"tasks": {}}, session, llm_adapter=fake_adapter
        )

    feeds_state = result["test-curate"]["feeds"]
    assert FEED_URL not in feeds_state, "failed feed must not get a state entry"
    assert FEED_B_URL in feeds_state
    assert feeds_state[FEED_B_URL]["last_run"]

    body = out_file.read_text()
    assert "B-Item About Gardening" in body
    assert "First Item About Cats" not in body


async def test_curate_filter_fails_open(mock_http, fake_adapter, tmp_path, monkeypatch):
    """When the filter LLM fails twice, ALL items pass through (fail-open)."""
    # Skip the 10s retry sleep inside _apply_llm_filter.
    import asyncio

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    mock_http.post(WEBHOOK_URL, status=204, repeat=True)

    # Filter call returns None twice → fail-open.
    fake_adapter.queue_structured(None)
    fake_adapter.queue_structured(None)

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"prompt": "irrelevant — filter will fail"},
    )

    async with aiohttp.ClientSession() as session:
        result = await _process_llm_curate_task(
            cfg, {"tasks": {}}, session, llm_adapter=fake_adapter
        )

    body = out_file.read_text()
    assert "First Item About Cats" in body
    assert "Second Item About Quantum Physics" in body

    items = result["test-curate"]["feeds"][FEED_URL]["items"]
    assert len(items) == 2
    # Per _merge_feed_state has_filter branch: items default to filter_pass=True
    # when the filter result didn't decorate them (which fail-open does).
    assert all(it["filter_pass"] is True for it in items)
