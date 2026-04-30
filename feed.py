import html.parser
import re
import urllib.request
import urllib.error
import logging
from dataclasses import dataclass

import feedparser

DESCRIPTION_MAX = 300

log = logging.getLogger(__name__)


@dataclass
class FeedEntry:
    id: str
    title: str
    link: str
    description: str
    image_url: str | None
    feed_title: str


class _TagStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


class _OGImageParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_image: str | None = None
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "body":
            self._done = True
        elif tag == "meta":
            d = dict(attrs)
            if d.get("property") == "og:image" and d.get("content"):
                self.og_image = d["content"]
                self._done = True


def _strip_html(text: str) -> str:
    p = _TagStripper()
    try:
        p.feed(text)
    except Exception:
        pass
    return p.get_text()


_MD_ESCAPE_RE = re.compile(r'(?m)(^[>#]+|[*_`~])')


def _escape_markdown(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r'\\\1', text)


def _fetch_og_image(url: str) -> str | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rss-discord/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            chunk = resp.read(32768).decode("utf-8", errors="replace")
        p = _OGImageParser()
        p.feed(chunk)
        return p.og_image
    except Exception as exc:
        log.debug("Could not fetch OG image from %s: %s", url, exc)
        return None


def get_new_entries(feed_cfg: dict, seen: set[str]) -> tuple[list[str], list[FeedEntry]]:
    """Fetch feed, return (current_ids, new_entries).

    current_ids: all entry IDs currently in the feed (for state update).
    new_entries: fully enriched FeedEntry list for unseen entries, chronological order.
    On parse failure returns (list(seen), []).
    """
    url = feed_cfg["url"]
    log.debug("Fetching feed: %s", url)
    parsed = feedparser.parse(url)

    if parsed.bozo and not parsed.entries:
        log.warning("Failed to parse feed %s: %s", url, parsed.bozo_exception)
        return list(seen), []

    feed_title = feed_cfg.get("name") or getattr(parsed.feed, "title", url)
    log.debug("[%s] Total entries in feed: %d", feed_title, len(parsed.entries))

    current_ids = []
    unseen_raw = []

    for entry in parsed.entries:
        eid = entry.get("id") or entry.get("link") or entry.get("title")
        if not eid:
            continue
        current_ids.append(eid)
        if eid not in seen:
            unseen_raw.append((eid, entry))
            log.debug("[%s] New entry: %s", feed_title, eid[:120])
        else:
            log.debug("[%s] Already seen: %s", feed_title, eid[:120])

    log.debug("[%s] New entries to post: %d", feed_title, len(unseen_raw))

    new_entries = []
    for eid, entry in reversed(unseen_raw):
        raw_desc = entry.get("summary") or entry.get("description", "")
        description = _strip_html(raw_desc).strip()
        if len(description) > DESCRIPTION_MAX:
            description = description[:DESCRIPTION_MAX].rstrip() + "…"
        description = _escape_markdown(description)

        link = entry.get("link", "")
        log.debug("[%s] Fetching OG image for %s", feed_title, link[:80])
        image_url = _fetch_og_image(link)
        log.debug("[%s] OG image: %s", feed_title, image_url)

        new_entries.append(FeedEntry(
            id=eid,
            title=entry.get("title", "(no title)").strip()[:256],
            link=link,
            description=description,
            image_url=image_url,
            feed_title=feed_title,
        ))

    return current_ids, new_entries
