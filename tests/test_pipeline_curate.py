"""E2E tests for the LLM curate (RSS) pipeline.

Real RSSSource, Discord*Target, FileItemTarget run. Only the LLM adapter
(passed as a parameter to _process_feed_task) and the aiohttp transport
(via aioresponses) are faked.
"""

import aiohttp

from tasks import process_feed_task
from tests.conftest import load_fixture, make_ctx, make_curate_cfg

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
        memory=[{"text": "Cat news today.", "citations": [1]}],
    )

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"criteria": "pass items about cats"},
    )

    async with aiohttp.ClientSession() as session:
        result = await process_feed_task(
            cfg, {"tasks": {}}, make_ctx(session, curate=fake_adapter, summarize=fake_adapter)
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

    ledger = result["test-curate"]["coverage"]["ledger"]
    assert len(ledger) == 1
    assert "Cat news today" in ledger[0]["state"]
    assert ledger[0]["frequency"] == 1


async def test_curate_dedup(mock_http, fake_adapter, tmp_path):
    """Items already in state are not re-pushed and not sent to the filter."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    mock_http.post(WEBHOOK_URL, status=204)

    fake_adapter.queue_filter(
        items=[{"id": 0, "pass": True, "reason": "Quantum is on-topic now."}],
        memory=[{"text": "Quantum news.", "citations": [0]}],
    )

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"criteria": "pass anything"},
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
                                "first_seen": "2026-05-01T00:00:00+00:00",
                            }
                        ],
                        "last_run": "2026-05-01T00:00:00+00:00",
                    }
                }
            }
        }
    }

    async with aiohttp.ClientSession() as session:
        result = await process_feed_task(
            cfg, state, make_ctx(session, curate=fake_adapter, summarize=fake_adapter)
        )

    body = out_file.read_text()
    assert "Second Item About Quantum Physics" in body
    assert "First Item About Cats" not in body

    feed_state = result["test-curate"]["feeds"][FEED_URL]
    assert feed_state["name"] == "Example"
    items = feed_state["items"]
    urls = {it["url"] for it in items}
    assert urls == {
        "https://example.com/posts/1",
        "https://example.com/posts/2",
    }
    assert all("first_seen" in it for it in items)
    assert not any("access_date" in it for it in items)


async def test_curate_pull_failure(mock_http, fake_adapter, tmp_path):
    """One feed returning HTTP error must NOT update its state; the other proceeds."""
    mock_http.get(FEED_URL, status=500)
    mock_http.get(FEED_B_URL, body=load_fixture("feed_b.xml"))
    mock_http.post(WEBHOOK_URL, status=204)

    fake_adapter.queue_filter(
        items=[{"id": 0, "pass": True, "reason": "All gardening passes."}],
        memory=[{"text": "Garden tips.", "citations": [0]}],
    )

    out_file = tmp_path / "out.md"
    cfg = make_curate_cfg(
        feeds=[
            {"url": FEED_URL, "name": "Example"},
            {"url": FEED_B_URL, "name": "Feed B"},
        ],
        file_path=str(out_file),
        llm_filter={"criteria": "pass anything"},
    )

    async with aiohttp.ClientSession() as session:
        result = await process_feed_task(
            cfg, {"tasks": {}}, make_ctx(session, curate=fake_adapter, summarize=fake_adapter)
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
        llm_filter={"criteria": "irrelevant — filter will fail"},
    )

    async with aiohttp.ClientSession() as session:
        result = await process_feed_task(
            cfg, {"tasks": {}}, make_ctx(session, curate=fake_adapter, summarize=fake_adapter)
        )

    body = out_file.read_text()
    assert "First Item About Cats" in body
    assert "Second Item About Quantum Physics" in body

    items = result["test-curate"]["feeds"][FEED_URL]["items"]
    assert len(items) == 2
    # Per _merge_feed_state has_filter branch: items default to filter_pass=True
    # when the filter result didn't decorate them (which fail-open does).
    assert all(it["filter_pass"] is True for it in items)


async def test_curate_passes_per_spec_reasoning_to_adapter(mock_http, fake_adapter, tmp_path):
    """Per-spec `reasoning: low` lands on the curate adapter's complete_structured call."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    mock_http.post(WEBHOOK_URL, status=204)

    fake_adapter.queue_filter(
        items=[
            {"id": 0, "pass": True, "reason": "ok"},
            {"id": 1, "pass": True, "reason": "ok"},
        ],
    )

    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(tmp_path / "out.md"),
        llm_filter={"criteria": "pass anything"},
    )

    async with aiohttp.ClientSession() as session:
        await process_feed_task(
            cfg,
            {"tasks": {}},
            make_ctx(session, curate=fake_adapter, summarize=fake_adapter, curate_reasoning="low"),
        )

    assert fake_adapter.structured_calls, "expected curate to make a structured call"
    assert fake_adapter.structured_calls[-1]["reasoning"] == "low"


async def test_analysis_forces_reasoning_over_spec(mock_http, fake_adapter, tmp_path):
    """`--analysis` (analysis=True) overrides per-spec reasoning with True."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    # No POST mocked: analysis is dry-run, no Discord call should happen.

    fake_adapter.queue_filter(
        items=[
            {"id": 0, "pass": True, "reason": "ok"},
            {"id": 1, "pass": True, "reason": "ok"},
        ],
    )

    cfg = make_curate_cfg(
        feeds=[{"url": FEED_URL, "name": "Example"}],
        llm_filter={"criteria": "pass anything"},
    )

    async with aiohttp.ClientSession() as session:
        await process_feed_task(
            cfg,
            {"tasks": {}},
            make_ctx(
                session,
                curate=fake_adapter,
                summarize=fake_adapter,
                curate_reasoning="off",
                analysis=True,
            ),
        )

    assert fake_adapter.structured_calls[-1]["reasoning"] is True


async def test_curate_agentic_corroborates(fake_adapter, monkeypatch):
    """Corroborate mode: the loop issues a search, finishes, then judges over a
    warm multi-turn conversation. Verdicts decode onto items as usual."""
    from pipeline import Item
    from process.curate import CurateAction, curate_items
    from providers.llm.base import ModelHandle

    searched: list[str] = []

    async def fake_search(query, *, max_results=10, region=None, site=None):
        searched.append(query)
        return [{"title": "Reuters", "snippet": "independently confirmed", "url": "https://r.com"}]

    monkeypatch.setattr("process._vasco.search", fake_search)

    items = [
        Item(id="u1", title="Major treaty signed", source="AP", url="https://a.com", body="x"),
        Item(id="u2", title="Local fair opens", source="AP", url="https://b.com", body="y"),
    ]
    # Loop sequence: search → finish → final FilterDecisions.
    fake_adapter.queue_structured(
        CurateAction(kind="search", queries=["treaty signed today"], rationale="verify")
    )
    fake_adapter.queue_structured(CurateAction(kind="finish", rationale="enough context"))
    fake_adapter.queue_filter(
        items=[
            {"id": 0, "pass": True, "reason": "Corroborated, globally significant."},
            {"id": 1, "pass": False, "reason": "Local human interest."},
        ],
        memory=[{"text": "A major treaty was signed.", "citations": [0]}],
    )

    cfg = {
        "criteria": "keep globally significant events",
        "corroborate": {"enabled": True, "max_steps": 3, "max_searches": 4},
    }
    handle = ModelHandle(fake_adapter, reasoning="off")

    result = await curate_items(items, cfg, handle, task_name="t")

    assert searched == ["treaty signed today"], "the loop's query reached vascod"
    by_url = {it.url: it for it in result.items}
    assert by_url["https://a.com"].filter_pass is True
    assert by_url["https://b.com"].filter_pass is False
    assert result.coverage and "treaty" in result.coverage[0].state
    # 2 action turns + 1 final verdict.
    assert len(fake_adapter.structured_calls) == 3
    # The final verdict was issued over a multi-turn conversation (warm prefix),
    # not a single prompt string.
    assert fake_adapter.structured_calls[-1]["messages"] is not None
    assert fake_adapter.structured_calls[-1]["prompt"] == ""
