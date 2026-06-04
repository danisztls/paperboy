"""Tests for entry `skip` (shorts/livestreams) + field `ignore` in pull.feed.get_new_entries.

`get_new_entries` receives the already-resolved feed cfg, so these pass the effective
`skip`/`ignore` blocks directly (tasks._pull_feeds is what merges the youtube-scope blocks
into them in production). YouTube channel RSS feeds surface Shorts as /shorts/<id> links and
regular videos as /watch?v=<id>; `skip.shorts` drops the /shorts/ entries from what gets posted
while keeping them in current_items (so they're recorded as seen)."""

from datetime import UTC, datetime, timedelta

import aiohttp

from config import is_youtube_feed_url, validate_config
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


async def test_skip_shorts_drops_shorts_keeps_them_seen(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    cfg = {"url": FEED_URL, "skip": {"shorts": True}}

    async with aiohttp.ClientSession() as session:
        _title, current_items, new_entries = await get_new_entries(cfg, set(), session)

    new_urls = {it.url for it in new_entries}
    assert new_urls == {WATCH_A, WATCH_B}, "the /shorts/ entry must not be posted"
    # still tracked in current_items so it is marked seen and not reconsidered
    assert SHORT in {ci["url"] for ci in current_items}


async def test_shorts_kept_when_skip_shorts_off(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    cfg = {"url": FEED_URL}  # no skip block

    async with aiohttp.ClientSession() as session:
        _title, _current_items, new_entries = await get_new_entries(cfg, set(), session)

    assert SHORT in {it.url for it in new_entries}


async def test_ignore_description_empties_body(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    cfg = {"url": FEED_URL, "ignore": {"description": True}}

    async with aiohttp.ClientSession() as session:
        _title, _current_items, new_entries = await get_new_entries(cfg, set(), session)

    assert new_entries, "entries should still be posted, just without a description"
    assert all(it.body == "" for it in new_entries)


def _watch_html(is_live: bool) -> str:
    flag = "true" if is_live else "false"
    return f'<html><body><script>var d = {{"isLiveContent":{flag}}};</script></body></html>'


async def test_skip_livestreams_drops_livestream_keeps_regular(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    mock_http.get(WATCH_A, body=_watch_html(is_live=True))  # livestream
    mock_http.get(WATCH_B, body=_watch_html(is_live=False))  # regular video
    # SHORT is intentionally NOT registered: it's a /shorts/ URL, so the livestream
    # stage must not fetch it. An unexpected fetch would raise in aioresponses.
    cfg = {"url": FEED_URL, "skip": {"livestreams": True}}

    async with aiohttp.ClientSession() as session:
        _title, current_items, new_entries = await get_new_entries(cfg, set(), session)

    new_urls = {it.url for it in new_entries}
    assert WATCH_A not in new_urls, "the livestream must not be posted"
    assert WATCH_B in new_urls, "the regular video must be posted"
    assert SHORT in new_urls, "shorts untouched when only skip.livestreams is set"
    assert WATCH_A in {ci["url"] for ci in current_items}, "livestream stays seen"


async def test_livestream_check_fails_open_on_fetch_error(mock_http):
    mock_http.get(FEED_URL, body=_feed_xml())
    mock_http.get(WATCH_A, exception=aiohttp.ClientError("boom"))
    mock_http.get(WATCH_B, body=_watch_html(is_live=False))
    cfg = {"url": FEED_URL, "skip": {"livestreams": True}}

    async with aiohttp.ClientSession() as session:
        _title, _current_items, new_entries = await get_new_entries(cfg, set(), session)

    # fetch error → not flagged as livestream → kept (better to post than silently drop)
    assert WATCH_A in {it.url for it in new_entries}


def test_is_youtube_feed_url_gates_scope():
    assert is_youtube_feed_url(FEED_URL)
    assert is_youtube_feed_url("https://www.youtube.com/feeds/videos.xml?channel_id=UCx")
    assert not is_youtube_feed_url("https://example.com/feed.xml")
    assert not is_youtube_feed_url("https://www.youtube.com/watch?v=x")


def test_validate_config_accepts_youtube_scope_at_all_levels():
    cfg = {
        "youtube": {"skip": {"shorts": True, "livestreams": True}, "ignore": {"description": True}},
        "tasks": [
            {
                "name": "t",
                "youtube": {"skip": {"shorts": True}},
                "pull": [
                    {"feed": {"url": "https://x/feed", "youtube": {"skip": {"shorts": False}}}}
                ],
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
