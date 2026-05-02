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
_CDATA_RE = re.compile(r'<!\[CDATA\[(.*?)\]\]>', re.DOTALL)


def _entry_title(entry) -> str:
    title = entry.get("title") or (entry.get("title_detail") or {}).get("value") or ""
    m = _CDATA_RE.search(title)
    return m.group(1).strip() if m else title.strip()


def _escape_markdown(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r'\\\1', text)


_BOT_DETECTION_THRESHOLD = 2048
_OG_FETCH_DELAY = 2.0

async def _fetch_og_image(url: str, session: aiohttp.ClientSession) -> tuple[str | None, bool]:
    """Single fetch attempt. Returns (og_image, bot_detected).

    bot_detected=True means the response was suspiciously small (<2kB), likely
    a challenge page — the caller should requeue rather than accept None as final.
    """
    if not url:
        return None, False
    try:
        async with session.get(url) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            chunk = await resp.content.read(32768)
        if status >= 400:
            log.debug("OG image fetch failed for %s: HTTP %d", url, status)
            return None, False
        if len(chunk) < _BOT_DETECTION_THRESHOLD:
            log.debug("OG image: got %d bytes (bot-detection?) from %s", len(chunk), url)
            return None, True
        text = chunk.decode("utf-8", errors="replace")
        log.debug("OG image: fetched %d bytes (status=%d, ct=%s) from %s", len(chunk), status, content_type, url)
        p = _OGImageParser()
        p.feed(text)
        if p.og_image:
            log.debug("OG image: found %s", p.og_image)
        else:
            log.debug("OG image: no og:image tag found in first %d bytes of %s", len(chunk), url)
        return p.og_image, False
    except Exception as exc:
        log.debug("OG image: exception fetching %s: %s: %s", url, type(exc).__name__, exc)
        return None, False


async def get_new_entries(
    feed_cfg: dict,
    seen: set[str],
    session: aiohttp.ClientSession,
    *,
    fetch_og_images: bool = True,
) -> tuple[list[dict], list[FeedEntry]] | None:
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

    current_items = []
    unseen_raw = []

    for entry in parsed.entries:
        eid = entry.get("link") or entry.get("title")
        if not eid:
            continue
        current_items.append({"url": entry.get("link", ""), "title": _entry_title(entry)})
        if eid not in seen:
            unseen_raw.append((eid, entry))
            log.debug("[%s] New entry: %s", feed_title, eid[:120])

    log.info("[%s] New entries to post: %d", feed_title, len(unseen_raw))

    # reverse to chronological order, then fetch OG images with a cooldown between requests.
    # bot-detected responses (<2kB) are appended to the end of the queue for one retry.
    ordered = list(reversed(unseen_raw))
    links = [e.get("link", "") for _, e in ordered]
    if fetch_og_images:
        og_results = [None] * len(links)
        queue = list(enumerate(links))  # (original_index, url)
        retry = []
        for i, (idx, link) in enumerate(queue):
            if i > 0:
                await asyncio.sleep(_OG_FETCH_DELAY)
            og, bot_detected = await _fetch_og_image(link, session)
            if bot_detected:
                log.debug("OG image: queuing %s for retry after remaining fetches", link)
                retry.append((idx, link))
            else:
                og_results[idx] = og
        for idx, link in retry:
            await asyncio.sleep(_OG_FETCH_DELAY)
            og, bot_detected = await _fetch_og_image(link, session)
            if bot_detected:
                log.debug("OG image: still bot-detected on retry for %s, giving up", link)
            og_results[idx] = og
    else:
        og_results = [None] * len(links)

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
            title=(_entry_title(entry) or "(no title)")[:256],
            link=link,
            description=description,
            image_url=image_url,
            feed_title=feed_title,
        ))

    return current_items, new_entries
