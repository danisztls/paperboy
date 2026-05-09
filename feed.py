import asyncio
import re
import logging
from datetime import datetime, timezone

import aiohttp
import feedparser
import feedparser.sanitizer
from bs4 import BeautifulSoup

from pipeline import Item, PullResult, Source

feedparser.sanitizer._HTMLSanitizer.acceptable_attributes.add("srcset")

DESCRIPTION_MAX = 512
ENTRY_MAX_AGE_SECONDS = 7 * 86400

log = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text()


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


def _url_filtered(url: str, cfg) -> bool:
    """Return True if the URL should be excluded by the url filter config."""
    if not cfg:
        return False
    if isinstance(cfg, list):
        return any(_url_filtered(url, item) for item in cfg)
    needles = cfg.get("skip_containing")
    if not needles:
        return False
    if isinstance(needles, str):
        needles = [needles]
    return any(n in url for n in needles)


def _apply_regex(cfg, text: str) -> str:
    if isinstance(cfg, list):
        for item in cfg:
            text = _apply_regex(item, text)
        return text
    if isinstance(cfg, dict):
        if cfg.get("clear"):
            return ""
        if cfg.get("remove_phrases_with_urls"):
            text = _remove_phrases_with_urls(text)
        if needle := cfg.get("remove_phrases_containing"):
            needles = needle if isinstance(needle, list) else [needle]
            for n in needles:
                text = re.sub(r'[^.!?\n]*?' + re.escape(n) + r'[^.!?\n]*', '', text)
            text = re.sub(r'[ \t]+', ' ', text).strip()
        if key := cfg.get("extract"):
            m = re.search(key, text)
            text = (m.group(1) if m.lastindex else m.group(0)) if m else text
        if "replace" in cfg:
            text = re.sub(cfg["replace"], cfg.get("with", ""), text)
        return text


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
                val = float(tokens[-1][:-1])
            except ValueError:
                pass
        if val > best_val:
            best_val, best_url = val, url
    return best_url


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
        img = BeautifulSoup(raw, "html.parser").find("img")
        if img:
            src = _best_srcset_url(img.get("srcset", "")) or img.get("src", "")
            if src and src.startswith("http"):
                return src
    return None


async def get_new_entries(
    feed_cfg: dict,
    seen: set[str],
    session: aiohttp.ClientSession,
) -> tuple[list[dict], list[Item]] | None:
    """Fetch feed, return (current_items, new_entries) or None on failure.

    current_items: all entries currently in the feed (url+title dicts, for state).
    new_entries: fully enriched Item list for unseen entries, chronological order.
    Returns None if the feed could not be parsed so callers skip the state write.
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

    feed_filter = feed_cfg.get("filter", {})
    filter_title = feed_filter.get("title")
    filter_description = feed_filter.get("description")
    filter_url = feed_filter.get("url")

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
            if _url_filtered(eid, filter_url):
                log.debug("[%s] URL-filtered: %s", feed_title, eid[:120])
            else:
                unseen_raw.append((eid, entry))
                log.debug("[%s] New entry: %s", feed_title, eid[:120])

    log.info("[%s] New entries to post: %d", feed_title, len(unseen_raw))

    ordered = list(reversed(unseen_raw))

    new_entries: list[Item] = []
    for eid, entry in ordered:
        link = entry.get("link", "")
        raw_desc = (
            entry.get("summary")
            or entry.get("description")
            or next((c.get("value", "") for c in entry.get("content", [])), "")
            or ""
        )
        body = _strip_html(raw_desc).strip()
        if filter_description:
            body = _apply_regex(filter_description, body)
        body = "\n".join(line for line in body.splitlines() if line.strip())
        if len(body) > DESCRIPTION_MAX:
            body = body[:DESCRIPTION_MAX].rstrip() + "…"
        body = _escape_markdown(body)

        pt = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*pt[:6], tzinfo=timezone.utc) if pt else None

        title = (_entry_title(entry) or "(no title)")[:256]
        if filter_title:
            title = _apply_regex(filter_title, title)

        new_entries.append(Item(
            id=eid,
            title=title,
            source=feed_title,
            url=link,
            body=body,
            image=_entry_image(entry),
            published=published,
        ))

    return current_items, new_entries


class RSSSource(Source):
    """Pulls items from an RSS/Atom feed."""

    async def pull(
        self,
        cfg: dict,
        seen: set[str],
        session,
    ) -> PullResult | None:
        result = await get_new_entries(cfg, seen, session)
        if result is None:
            return None
        current_items, new_items = result
        return PullResult(new_items=new_items, current_items=current_items)
