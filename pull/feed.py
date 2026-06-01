import asyncio
import logging
import re
from datetime import UTC, datetime

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from pipeline import Item, PullResult, Source
from process.filter_heuristic import apply_regex, url_filtered

DESCRIPTION_MAX = 512
DEFAULT_ENTRY_MAX_AGE_SECONDS = 7 * 86400

log = logging.getLogger(__name__)


_MD_ESCAPE_RE = re.compile(r"(?m)(^[>#]+|[*_`~])")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _entry_title(entry) -> str:
    title = entry.get("title") or (entry.get("title_detail") or {}).get("value") or ""
    m = _CDATA_RE.search(title)
    return m.group(1).strip() if m else title.strip()


def _escape_markdown(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


def _entry_image(entry) -> str | None:
    for thumb in entry.get("media_thumbnail", []):
        url = thumb.get("url", "")
        if url:
            return url
    for media in entry.get("media_content", []):
        if media.get("medium") == "image" or media.get("type", "").startswith("image/"):
            url = media.get("url", "")
            if url:
                return url
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/"):
            url = enc.get("url", "")
            if url:
                return url
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image/"):
            href = link.get("href", "")
            if href:
                return href
    return None


async def get_new_entries(
    feed_cfg: dict,
    seen: set[str],
    session: aiohttp.ClientSession,
    filter_log: dict | None = None,
    *,
    max_age_seconds: int = DEFAULT_ENTRY_MAX_AGE_SECONDS,
) -> tuple[str, list[dict], list[Item]] | None:
    """Fetch feed, return (feed_title, current_items, new_entries) or None on failure.

    feed_title: resolved display name of the feed (cfg name → feed title → url).
    current_items: all entries currently in the feed (url/title/source_date dicts, for state).
    new_entries: fully enriched Item list for unseen entries, chronological order.
    Returns None if the feed could not be parsed so callers skip the state write.
    """
    url = feed_cfg["url"]
    log.debug("Fetching feed: %s", url)
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

    now = datetime.now(UTC)
    _new_eligible = 0
    for entry in parsed.entries:
        eid = entry.get("link")
        if not eid:
            continue
        pt = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*pt[:6], tzinfo=UTC) if pt else None
        if published and (now - published).total_seconds() > max_age_seconds:
            log.debug("[%s] Skipping old entry (%s): %s", feed_title, published.date(), eid[:80])
            continue
        ci = {"url": entry.get("link", ""), "title": _entry_title(entry)}
        if published:
            ci["source_date"] = published.isoformat()
        current_items.append(ci)
        if eid not in seen:
            _new_eligible += 1
            if url_filtered(eid, filter_url):
                log.debug("[%s] URL-filtered: %s", feed_title, eid[:120])
                if filter_log is not None:
                    filter_log["url_excluded"].append({"url": eid})
            else:
                unseen_raw.append((eid, entry))
                log.debug("[%s] New entry: %s", feed_title, eid[:120])

    if filter_log is not None:
        filter_log["total_in_feed"] = len(current_items)
        filter_log["new_eligible"] = _new_eligible

    log.debug("[%s] New entries to post: %d", feed_title, len(unseen_raw))

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
        body_soup = BeautifulSoup(raw_desc, "html.parser")
        body = body_soup.get_text().strip()
        if filter_description:
            _orig_body = body
            body = apply_regex(filter_description, body)
            if filter_log is not None and body != _orig_body:
                filter_log["description_transforms"].append(
                    {"id": eid, "before": _orig_body[:300], "after": body[:300]}
                )
        body = "\n".join(line for line in body.splitlines() if line.strip())
        if len(body) > DESCRIPTION_MAX:
            body = body[:DESCRIPTION_MAX].rstrip() + "…"
        body = _escape_markdown(body)

        pt = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*pt[:6], tzinfo=UTC) if pt else None

        title = (_entry_title(entry) or "(no title)")[:256]
        if filter_title:
            _orig_title = title
            title = apply_regex(filter_title, title)
            if filter_log is not None and title != _orig_title:
                filter_log["title_transforms"].append(
                    {"id": eid, "before": _orig_title, "after": title}
                )

        new_entries.append(
            Item(
                id=eid,
                title=title,
                source=feed_title,
                url=link,
                body=body,
                image=_entry_image(entry),
                published=published,
            )
        )

    return feed_title, current_items, new_entries


class RSSSource(Source):
    """Pulls items from an RSS/Atom feed."""

    def __init__(self, max_age_seconds: int = DEFAULT_ENTRY_MAX_AGE_SECONDS) -> None:
        self._max_age_seconds = max_age_seconds

    async def pull(
        self,
        cfg: dict,
        seen: set[str],
        session,
        *,
        filter_log: dict | None = None,
    ) -> PullResult | None:
        result = await get_new_entries(
            cfg, seen, session, filter_log=filter_log, max_age_seconds=self._max_age_seconds
        )
        if result is None:
            return None
        feed_title, current_items, new_items = result
        return PullResult(new_items=new_items, current_items=current_items, name=feed_title)
