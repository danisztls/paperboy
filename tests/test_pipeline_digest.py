"""E2E test for the digest pipeline (summarize → filter → DiscordDigest + FileDigest)."""

import json

import aiohttp

from tasks import _process_feed_task
from tests.conftest import load_fixture, make_curate_cfg

FEED_URL = "https://feed.example/rss"
WEBHOOK_URL = "https://discord.example/webhook"


async def test_digest_cite_map(mock_http, fake_adapter, tmp_path):
    """Digest task: structured memory paragraphs with citation IDs resolve to markdown
    links in the file and Discord masked links in the webhook POST. Stored memory
    contains only plain text (no citation markers)."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    # Article URLs hit by fetch_item_content during summarize.
    article_html = load_fixture("article_basic.html")
    mock_http.get("https://example.com/posts/1", body=article_html)
    mock_http.get("https://example.com/posts/2", body=article_html)
    mock_http.post(WEBHOOK_URL, status=204, repeat=True)

    # 2 summarize responses (one per item, concurrent so order doesn't matter)
    # + 1 filter response with two structured memory paragraphs.
    fake_adapter.queue_text("Summary of the first article.")
    fake_adapter.queue_text("Summary of the second article.")
    fake_adapter.queue_filter(
        items=[
            {"id": 0, "pass": True, "reason": "Cats."},
            {"id": 1, "pass": True, "reason": "Quantum."},
        ],
        memory=[
            {"text": "Cat update.", "citations": [0]},
            {"text": "Quantum advance.", "citations": [1]},
        ],
    )

    out_file = tmp_path / "digest.md"
    cfg = make_curate_cfg(
        kind="digest",
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"criteria": "pass everything"},
    )

    async with aiohttp.ClientSession() as session:
        result = await _process_feed_task(
            cfg, {"tasks": {}}, session, curate_adapter=fake_adapter, summarize_adapter=fake_adapter
        )

    body = out_file.read_text()
    # Citations resolved to markdown links by FileDigestTarget.
    assert "[Example](https://example.com/posts/1)" in body
    assert "[Example](https://example.com/posts/2)" in body

    # Stored memory contains only plain paragraph text — no citation markers.
    memory_log = result["test-curate"]["memory"]
    assert len(memory_log) == 1
    entry = next(iter(memory_log.values()))
    assert "Cat update" in entry
    assert "Quantum advance" in entry

    # Real DiscordDigestTarget posted the digest chunk(s).
    post_calls = [
        call for key, calls in mock_http.requests.items() if key[0] == "POST" for call in calls
    ]
    assert len(post_calls) >= 1
    contents = [json.loads(call.kwargs["data"].decode())["content"] for call in post_calls]
    # Both stories landed in the same chunk, separated by a blank line (one paragraph each).
    assert any("Cat update" in c and "\n\nQuantum advance" in c for c in contents)
    joined = "\n\n".join(contents)
    assert "[[Example](<https://example.com/posts/1>)]" in joined
    assert "[[Example](<https://example.com/posts/2>)]" in joined


async def test_digest_sections(mock_http, fake_adapter, tmp_path):
    """Section headings in structured memory render as ## in Discord (glued to their
    paragraph) and as ### in the file. A heading is never orphaned into a separate chunk."""
    mock_http.get(FEED_URL, body=load_fixture("feed_basic.xml"))
    article_html = load_fixture("article_basic.html")
    mock_http.get("https://example.com/posts/1", body=article_html)
    mock_http.get("https://example.com/posts/2", body=article_html)
    mock_http.post(WEBHOOK_URL, status=204, repeat=True)

    fake_adapter.queue_text("Summary of the first article.")
    fake_adapter.queue_text("Summary of the second article.")
    fake_adapter.queue_filter(
        items=[
            {"id": 0, "pass": True, "reason": "Cats."},
            {"id": 1, "pass": True, "reason": "Quantum."},
        ],
        memory=[
            {"section": "Brasil", "text": "Cat update.", "citations": [0]},
            {"section": "Geopolítica", "text": "Quantum advance.", "citations": [1]},
        ],
    )

    out_file = tmp_path / "digest.md"
    cfg = make_curate_cfg(
        kind="digest",
        feeds=[{"url": FEED_URL, "name": "Example"}],
        file_path=str(out_file),
        llm_filter={"criteria": "pass everything"},
    )

    async with aiohttp.ClientSession() as session:
        await _process_feed_task(
            cfg,
            {"tasks": {}},
            session,
            curate_adapter=fake_adapter,
            summarize_adapter=fake_adapter,
        )

    # File: section headings render as ### markdown headings.
    body = out_file.read_text()
    assert "### Brasil\n\n" in body
    assert "### Geopolítica\n\n" in body

    # Discord: section header is in the same chunk as its paragraph — never orphaned.
    post_calls = [
        call for key, calls in mock_http.requests.items() if key[0] == "POST" for call in calls
    ]
    contents = [json.loads(call.kwargs["data"].decode())["content"] for call in post_calls]
    assert any("## Brasil\n\n" in c and "Cat update." in c for c in contents)
    assert any("## Geopolítica\n\n" in c and "Quantum advance." in c for c in contents)
