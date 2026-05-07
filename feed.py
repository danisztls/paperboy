import asyncio
import html.parser
import re
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
import feedparser
import feedparser.sanitizer

feedparser.sanitizer._HTMLSanitizer.acceptable_attributes.add("srcset")

DESCRIPTION_MAX = 300
ENTRY_MAX_AGE_SECONDS = 7 * 86400

log = logging.getLogger(__name__)


@dataclass
class FeedEntry:
    id: str
    title: str
    link: str
    description: str
    feed_title: str
    image: str | None = None
    published: datetime | None = None


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
_PHRASE_URL_RE = re.compile(r'[^.!?\n]*?https?://\S+')


def _entry_title(entry) -> str:
    title = entry.get("title") or (entry.get("title_detail") or {}).get("value") or ""
    m = _CDATA_RE.search(title)
    return m.group(1).strip() if m else title.strip()


def _escape_markdown(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r'\\\1', text)


def _remove_phrases_with_urls(text: str) -> str:
    text = _PHRASE_URL_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _apply_regex(cfg, text: str) -> str:
    if isinstance(cfg, list):
        for item in cfg:
            text = _apply_regex(item, text)
        return text
    if isinstance(cfg, dict):
        if cfg.get("remove_phrases_with_urls"):
            return _remove_phrases_with_urls(text)
        if needle := cfg.get("remove_phrases_containing"):
            needles = needle if isinstance(needle, list) else [needle]
            for n in needles:
                text = re.sub(r'[^.!?\n]*?' + re.escape(n) + r'[^.!?\n]*', '', text)
            return re.sub(r'[ \t]+', ' ', text).strip()
        if key := cfg.get("extract"):
            m = re.search(key, text)
            return (m.group(1) if m.lastindex else m.group(0)) if m else text
        return re.sub(cfg["replace"], cfg.get("with", ""), text)


def _best_srcset_url(srcset: str) -> str | None:
    best_url, best_val = None, -1.0
    for part in srcset.split(","):
        tokens = part.strip().split()
        if not tokens or not tokens[0].startswith("http"):
            continue
        url = tokens[0]
        val = 1.0
        if len(tokens) > 1:
            try:
                val = float(tokens[-1][:-1])  # strip trailing 'x' or 'w'
            except ValueError:
                pass
        if val > best_val:
            best_val, best_url = val, url
    return best_url


class _ImgSrcParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.src: str | None = None

    def handle_starttag(self, tag, attrs):
        if self.src or tag != "img":
            return
        d = dict(attrs)
        src = _best_srcset_url(d.get("srcset", "")) or d.get("src", "")
        if src.startswith("http"):
            self.src = src


def _entry_image(entry) -> str | None:
    for thumb in entry.get("media_thumbnail", []):
        url = thumb.get("url", "")
        if url:
            return url
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/"):
            url = enc.get("url", "")
            if url:
                return url
    raw = entry.get("summary") or entry.get("description", "")
    if raw:
        p = _ImgSrcParser()
        try:
            p.feed(raw)
        except Exception:
            pass
        if p.src:
            return p.src
    return None


async def get_new_entries(
    feed_cfg: dict,
    seen: set[str],
    session: aiohttp.ClientSession,
) -> tuple[list[dict], list[FeedEntry]] | None:
    """Fetch feed, return (current_ids, new_entries) or None on failure.

    current_ids: all entry IDs currently in the feed (for state update).
    new_entries: fully enriched FeedEntry list for unseen entries, chronological order.
    Returns None if the feed could not be parsed, so the caller can avoid
    overwriting state (including the last_run timestamp) for a broken fetch.
    """
    url = feed_cfg["url"]
    log.info("Fetching feed: %s", url)
    try:
        async with session.get(url) as resp:
            if resp.status >= 400:
                log.warning("Feed fetch failed %s: HTTP %d", url, resp.status)
                return None
            content = await resp.read()
    except aiohttp.ClientError as exc:
        log.warning("Feed fetch error %s: %s", url, exc)
        return None
    parsed = await asyncio.to_thread(
        feedparser.parse, content, response_headers={"content-location": url}
    )

    if parsed.bozo and not parsed.entries:
        log.warning("Failed to parse feed %s: %s", url, parsed.bozo_exception)
        return None

    feed_title = feed_cfg.get("name") or getattr(parsed.feed, "title", url)
    log.debug("[%s] Total entries in feed: %d", feed_title, len(parsed.entries))

    current_items = []
    unseen_raw = []

    now = datetime.now(timezone.utc)
    for entry in parsed.entries:
        eid = entry.get("link")
        if not eid:
            continue
        pt = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*pt[:6], tzinfo=timezone.utc) if pt else None
        if published and (now - published).total_seconds() > ENTRY_MAX_AGE_SECONDS:
            log.debug("[%s] Skipping old entry (%s): %s", feed_title, published.date(), eid[:80])
            continue
        current_items.append({"url": entry.get("link", ""), "title": _entry_title(entry)})
        if eid not in seen:
            unseen_raw.append((eid, entry))
            log.debug("[%s] New entry: %s", feed_title, eid[:120])

    log.info("[%s] New entries to post: %d", feed_title, len(unseen_raw))

    ordered = list(reversed(unseen_raw))

    feed_filter = feed_cfg.get("filter", {})
    filter_title = feed_filter.get("title")
    filter_description = feed_filter.get("description")

    new_entries = []
    for eid, entry in ordered:
        link = entry.get("link", "")
        raw_desc = (
            entry.get("summary")
            or entry.get("description")
            or next((c.get("value", "") for c in entry.get("content", [])), "")
            or ""
        )
        description = _strip_html(raw_desc).strip()
        if filter_description:
            description = _apply_regex(filter_description, description)
        if len(description) > DESCRIPTION_MAX:
            description = description[:DESCRIPTION_MAX].rstrip() + "…"
        description = _escape_markdown(description)

        pt = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*pt[:6], tzinfo=timezone.utc) if pt else None

        fe = FeedEntry(
            id=eid,
            title=(_entry_title(entry) or "(no title)")[:256],
            link=link,
            description=description,
            feed_title=feed_title,
            image=_entry_image(entry),
            published=published,
        )
        if filter_title:
            fe.title = _apply_regex(filter_title, fe.title)
        new_entries.append(fe)

    return current_items, new_entries
