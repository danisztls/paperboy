"""Tests for YouTube `youtube.ignore_shorts` filtering in pull.feed.get_new_entries.

YouTube channel RSS feeds surface Shorts as /shorts/<id> links and regular videos
as /watch?v=<id>. `ignore_shorts` drops the /shorts/ entries from what gets posted
while keeping them in current_items (so they're recorded as seen).
"""

from datetime import UTC, datetime, timedelta

import aiohttp

from config import validate_config
from pull.feed import get_new_entries

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtest"
WATCH_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
SHORT = "https://www.youtube.com/shorts/sssssssssss"
WATCH_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def _entry(title: str, href: str, when: datetime) -> str:
    iso = when.replace(microsecond=0).isoformat()
    return f"""  <entry>
    <id>yt:video:{href.rsplit("=", 1)[-1].rsplit("/", 1)[-1]}</id>
    <title>{title}</title>
    <link rel="alternate" href="{href}"/>
    <published>{iso}</published>
    <updated>{iso}</updated>
  </entry>"""


def _feed_xml() -> bytes:
    now = datetime.now(UTC)
    entries = "\n".join(
        [
            _entry("Regular video A", WATCH_A, now - timedelta(minutes=30)),
            _entry("A short", SHORT, now - timedelta(minutes=20)),
            _entry("Regular video B", WATCH_B, now - timedelta(minutes=10)),
        ]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <title>Test Channel</title>
{entries}
</feed>""".encode()


async def test_ignore_shorts_drops_shorts_keeps_them_seen(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    cfg = {"url": FEED_URL, "youtube": {"ignore_shorts": True}}

    async with aiohttp.ClientSession() as session:
        _title, current_items, new_entries = await get_new_entries(cfg, set(), session)

    new_urls = {it.url for it in new_entries}
    assert new_urls == {WATCH_A, WATCH_B}, "the /shorts/ entry must not be posted"
    # still tracked in current_items so it is marked seen and not reconsidered
    assert SHORT in {ci["url"] for ci in current_items}


async def test_shorts_kept_when_ignore_shorts_off(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    cfg = {"url": FEED_URL}  # no youtube block

    async with aiohttp.ClientSession() as session:
        _title, _current_items, new_entries = await get_new_entries(cfg, set(), session)

    assert SHORT in {it.url for it in new_entries}


def test_validate_config_accepts_youtube_at_all_levels():
    cfg = {
        "youtube": {"ignore_shorts": True, "ignore_livestreams": True},
        "tasks": [
            {
                "name": "t",
                "youtube": {"ignore_shorts": True},
                "pull": [{"feed": {"url": "https://x/feed", "youtube": {"ignore_shorts": False}}}],
                "push": [{"discord": {"webhook": "https://d/wh"}}],
            }
        ],
    }
    assert validate_config(cfg) == []


def test_validate_config_rejects_unknown_youtube_key():
    cfg = {
        "youtube": {"ignore_typo": True},
        "tasks": [
            {
                "name": "t",
                "pull": [{"feed": {"url": "https://x/feed"}}],
                "push": [{"discord": {"webhook": "https://d/wh"}}],
            }
        ],
    }
    errors = validate_config(cfg)
    assert any("ignore_typo" in e or "youtube" in e for e in errors)
