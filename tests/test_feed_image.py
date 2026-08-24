# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Unit tests for `pull.feed._entry_image` resolution chain.

feedparser entries are dict-like; the function only uses `.get()` and iteration,
so plain dicts stand in for FeedParserDict entries.
"""

from pull.feed import _entry_image


def test_media_thumbnail_wins_over_everything():
    entry = {
        "media_thumbnail": [{"url": "https://example.com/thumb.jpg"}],
        "media_content": [{"medium": "image", "url": "https://example.com/content.jpg"}],
        "enclosures": [{"type": "image/png", "url": "https://example.com/enc.png"}],
    }
    assert _entry_image(entry) == "https://example.com/thumb.jpg"


def test_media_content_medium_image():
    entry = {"media_content": [{"medium": "image", "url": "https://example.com/content.jpg"}]}
    assert _entry_image(entry) == "https://example.com/content.jpg"


def test_media_content_image_type():
    entry = {"media_content": [{"type": "image/jpeg", "url": "https://example.com/content.jpg"}]}
    assert _entry_image(entry) == "https://example.com/content.jpg"


def test_media_content_non_image_skipped():
    entry = {
        "media_content": [
            {"medium": "video", "url": "https://example.com/clip.mp4"},
            {"medium": "image", "url": "https://example.com/content.jpg"},
        ]
    }
    assert _entry_image(entry) == "https://example.com/content.jpg"


def test_enclosure_image():
    entry = {"enclosures": [{"type": "image/png", "url": "https://example.com/enc.png"}]}
    assert _entry_image(entry) == "https://example.com/enc.png"


def test_link_enclosure_image():
    entry = {
        "links": [
            {"rel": "alternate", "type": "text/html", "href": "https://example.com/post"},
            {"rel": "enclosure", "type": "image/jpeg", "href": "https://example.com/link.jpg"},
        ]
    }
    assert _entry_image(entry) == "https://example.com/link.jpg"


def test_empty_entry_returns_none():
    assert _entry_image({}) is None
