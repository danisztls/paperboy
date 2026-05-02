import asyncio
import html.parser
import re
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
    feed_title: str


class _TagStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


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


async def get_new_entries(
    feed_cfg: dict,
    seen: set[str],
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

    ordered = list(reversed(unseen_raw))

    new_entries = []
    for eid, entry in ordered:
        link = entry.get("link", "")
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
            feed_title=feed_title,
        ))

    return current_items, new_entries
