import asyncio
import html.parser
import re
import logging
from dataclasses import dataclass

import aiohttp
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


async def _fetch_og_image(url: str, session: aiohttp.ClientSession) -> str | None:
    if not url:
        return None
    try:
        async with session.get(url) as resp:
            chunk = await resp.content.read(32768)
            text = chunk.decode("utf-8", errors="replace")
        p = _OGImageParser()
        p.feed(text)
        return p.og_image
    except Exception as exc:
        log.debug("Could not fetch OG image from %s: %s", url, exc)
        return None


async def get_new_entries(
    feed_cfg: dict,
    seen: set[str],
    session: aiohttp.ClientSession,
) -> tuple[list[str], list[FeedEntry]] | None:
    """Fetch feed, return (current_ids, new_entries) or None on failure.

    current_ids: all entry IDs currently in the feed (for state update).
    new_entries: fully enriched FeedEntry list for unseen entries, chronological order.
    Returns None if the feed could not be parsed, so the caller can avoid
    overwriting state (including the last_run timestamp) for a broken fetch.
    """
    url = feed_cfg["url"]
    log.info("Fetching feed: %s", url)
    parsed = await asyncio.to_thread(feedparser.parse, url)

    if parsed.bozo and not parsed.entries:
        log.warning("Failed to parse feed %s: %s", url, parsed.bozo_exception)
        return None

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

    log.info("[%s] New entries to post: %d", feed_title, len(unseen_raw))

    # reverse to chronological order, then enrich all OG images concurrently
    ordered = list(reversed(unseen_raw))
    links = [e.get("link", "") for _, e in ordered]
    og_results = await asyncio.gather(*[_fetch_og_image(link, session) for link in links])

    new_entries = []
    for (eid, entry), link, image_url in zip(ordered, links, og_results):
        log.debug("[%s] OG image for %s: %s", feed_title, link[:80], image_url)

        raw_desc = entry.get("summary") or entry.get("description", "")
        description = _strip_html(raw_desc).strip()
        if len(description) > DESCRIPTION_MAX:
            description = description[:DESCRIPTION_MAX].rstrip() + "…"
        description = _escape_markdown(description)

        new_entries.append(FeedEntry(
            id=eid,
            title=entry.get("title", "(no title)").strip()[:256],
            link=link,
            description=description,
            image_url=image_url,
            feed_title=feed_title,
        ))

    return current_ids, new_entries
