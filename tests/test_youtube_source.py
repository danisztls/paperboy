"""Tests for the `youtube` pull source — sugar over `feed` expanded in config.get_feeds."""

import aiohttp

from config import get_feeds, task_kind, validate_config
from tasks import _process_feed_task
from tests.conftest import make_curate_cfg

CHANNEL = "UCBa659QWEk1AI4Tg--mrJ2A"
EXPECTED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"


def test_get_feeds_expands_youtube_item():
    task = {
        "pull": [
            {
                "youtube": {
                    "name": "Tom Scott",
                    "channel_id": CHANNEL,
                    "summarize": False,
                    "curate": {"skip": True},
                    "ignore_livestreams": False,
                }
            }
        ]
    }
    feeds = get_feeds(task)
    assert len(feeds) == 1
    feed = feeds[0]
    assert feed["url"] == EXPECTED_URL
    assert feed["name"] == "Tom Scott"
    assert feed["summarize"] is False
    assert feed["curate"] == {"skip": True}
    # flat ignore_* move into the feed's youtube filter block; channel_id is not leaked
    assert feed["youtube"] == {"ignore_livestreams": False}
    assert "channel_id" not in feed
    assert "ignore_livestreams" not in feed


def test_get_feeds_no_youtube_block_when_no_ignore_keys():
    feeds = get_feeds({"pull": [{"youtube": {"name": "X", "channel_id": CHANNEL}}]})
    assert "youtube" not in feeds[0]


def test_get_feeds_mixes_feed_and_youtube_in_order():
    task = {
        "pull": [
            {"feed": {"name": "A", "url": "https://a.example/rss"}},
            {"youtube": {"name": "B", "channel_id": CHANNEL}},
        ]
    }
    feeds = get_feeds(task)
    assert [f["url"] for f in feeds] == ["https://a.example/rss", EXPECTED_URL]


def test_task_kind_youtube_only_is_feeds():
    task = {"pull": [{"youtube": {"channel_id": CHANNEL}}]}
    assert task_kind(task) == "feeds"


def _task_with_pull(pull: list[dict]) -> dict:
    return {"name": "t", "pull": pull, "push": [{"discord": {"webhook": "https://d/wh"}}]}


def test_validate_config_accepts_youtube_source():
    cfg = {
        "youtube": {"ignore_shorts": True},  # global filter block coexists with the source
        "tasks": [
            _task_with_pull(
                [
                    {"youtube": {"name": "B", "channel_id": CHANNEL, "ignore_shorts": False}},
                    {"feed": {"url": "https://a.example/rss"}},
                ]
            )
        ],
    }
    assert validate_config(cfg) == []


def test_validate_config_rejects_missing_channel_id():
    cfg = {"tasks": [_task_with_pull([{"youtube": {"name": "B"}}])]}
    assert any("channel_id" in e for e in validate_config(cfg))


def test_validate_config_rejects_unknown_youtube_source_key():
    cfg = {"tasks": [_task_with_pull([{"youtube": {"channel_id": CHANNEL, "bogus": 1}}])]}
    assert any("bogus" in e or "youtube" in e for e in validate_config(cfg))


def test_validate_config_rejects_mixing_youtube_with_search():
    cfg = {
        "tasks": [
            _task_with_pull(
                [
                    {"youtube": {"channel_id": CHANNEL}},
                    {"search": {"prompt": "x"}},
                ]
            )
        ]
    }
    assert validate_config(cfg) != []


async def test_youtube_source_fetches_constructed_url(mock_http, fake_adapter, tmp_path):
    """A task with a youtube pull item fetches the built feed URL end-to-end."""
    feed_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Tom Scott</title>
  <entry>
    <title>A video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=zzzzzzzzzzz"/>
  </entry>
</feed>"""
    mock_http.get(EXPECTED_URL, body=feed_xml)
    mock_http.post("https://discord.example/webhook", status=204)

    cfg = make_curate_cfg(feeds=[])  # start from a normal task, swap in a youtube pull item
    cfg["pull"] = [{"youtube": {"name": "Tom Scott", "channel_id": CHANNEL, "summarize": False}}]
    cfg.pop("curate", None)

    async with aiohttp.ClientSession() as session:
        result = await _process_feed_task(
            cfg, {"tasks": {}}, session, summarize_adapter=fake_adapter
        )

    feeds_state = result["test-curate"]["feeds"]
    assert EXPECTED_URL in feeds_state
    assert any(
        it["url"].endswith("watch?v=zzzzzzzzzzz") for it in feeds_state[EXPECTED_URL]["items"]
    )
