# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Unit tests for process.filter_heuristic.apply_regex / url_filtered."""

from process.filter_heuristic import apply_regex, url_filtered

# hnrss item description, post HTML-strip (BeautifulSoup get_text): newline-separated lines.
HN_BODY = (
    "Article URL: https://blog.cloudflare.com/voidzero-joins-cloudflare/\n"
    "Comments URL: https://news.ycombinator.com/item?id=48398055\n"
    "Points: 530\n"
    "# Comments: 238"
)


def test_remove_list_reproduces_hn_strip():
    """`remove` (list of regexes) replaces the old remove_phrases_containing on the HN feed."""
    out = apply_regex({"remove": ["Points:.*", "# Comments:.*"]}, HN_BODY)
    assert "Points:" not in out
    assert "# Comments:" not in out
    # The URL lines survive — neither pattern false-matches "Article URL:" / "Comments URL:".
    assert "Article URL: https://blog.cloudflare.com/voidzero-joins-cloudflare/" in out
    assert "Comments URL: https://news.ycombinator.com/item?id=48398055" in out


def test_remove_scalar_regex():
    assert apply_regex({"remove": r"\d+"}, "a1b2c3") == "abc"


def test_extract_group():
    assert apply_regex({"extract": r"(\d+) de \w+"}, "foo 12 de maio bar") == "12"


def test_replace_with():
    assert apply_regex({"replace": r"Ad:\s*", "with": ""}, "Ad: hello world") == "hello world"


def test_non_dict_cfg_passthrough():
    assert apply_regex(None, "x") == "x"


def test_url_filtered():
    assert url_filtered("https://x/shorts/abc", "/shorts/")
    assert url_filtered("https://x/shorts/abc", ["/live/", "/shorts/"])
    assert not url_filtered("https://x/watch?v=1", ["/shorts/"])
    assert not url_filtered("https://x/watch", None)
