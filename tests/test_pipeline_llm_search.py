"""E2E test for the LLM search pipeline (LLM call → DiscordText + FileEmbed)."""

import aiohttp

from tasks import _process_search_task
from tests.conftest import make_search_cfg

WEBHOOK_URL = "https://discord.example/webhook"


async def test_llm_search_happy(mock_http, fake_adapter, tmp_path):
    """LLM search produces a single Item posted as plain text + written to file."""
    mock_http.post(WEBHOOK_URL, status=204)

    fake_adapter.queue_text("hello world")

    out_file = tmp_path / "out.md"
    cfg = make_search_cfg(file_path=str(out_file))

    async with aiohttp.ClientSession() as session:
        result = await _process_search_task(
            cfg, {"tasks": {}}, session, search_adapter=fake_adapter
        )

    assert result == {"test-search": {"last_run": result["test-search"]["last_run"]}}
    assert result["test-search"]["last_run"]

    assert "hello world" in out_file.read_text()

    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 1
