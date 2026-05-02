import asyncio
import html.parser
import io
import json
import logging
import re

import aiohttp

from feed import FeedEntry

log = logging.getLogger(__name__)

_BOT_DETECTION_THRESHOLD = 2048
_OG_FETCH_DELAY = 2.0


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


async def _fetch_og_image_once(url: str, session: aiohttp.ClientSession) -> tuple[str | None, bool]:
    """Single attempt. Returns (og_image_url, bot_detected)."""
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


async def _fetch_og_image(url: str, session: aiohttp.ClientSession) -> str | None:
    """Fetch og:image URL from article HTML, with one bot-detection retry."""
    og, bot_detected = await _fetch_og_image_once(url, session)
    if bot_detected:
        log.debug("OG image: bot-detected, retrying after %.0f s for %s", _OG_FETCH_DELAY, url)
        await asyncio.sleep(_OG_FETCH_DELAY)
        og, _ = await _fetch_og_image_once(url, session)
    return og

_MAX_BYTES = 4 * 1024 * 1024
_MAX_DIM = 2000


async def _fetch_image(url: str, session: aiohttp.ClientSession) -> bytes | None:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if not ct.startswith("image/"):
                return None
            return await resp.read()
    except Exception as exc:
        log.debug("Could not download image %s: %s", url, exc)
        return None


def _optimize_image(data: bytes) -> bytes | None:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        needs_resize = max(img.size) > _MAX_DIM
        if len(data) <= _MAX_BYTES and not needs_resize:
            return data
        if needs_resize:
            img.thumbnail((_MAX_DIM, _MAX_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=85)
        result = buf.getvalue()
        return result if len(result) <= 8 * 1024 * 1024 else None
    except Exception as exc:
        log.debug("Could not optimize image: %s", exc)
        return None


async def post_text_to_discord(
    webhook_url: str,
    text: str,
    session: aiohttp.ClientSession,
) -> None:
    if len(text) > 2000:
        text = text[:1997] + "…"
    payload = json.dumps({"content": text}).encode()
    log.debug("Posting text to Discord (%d chars)", len(text))
    try:
        async with session.post(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            log.debug("Discord response: %s", resp.status)
            if resp.status not in (200, 204):
                log.warning("Unexpected Discord response: %s", resp.status)
            if resp.status >= 400:
                body = await resp.text()
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=body,
                )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


_CONTENT_LIMIT = 2000
_CITE_RE = re.compile(r'\[(\d+)\]')
_CONSEC_CITE_RE = re.compile(r'\)\s*(\[\[)')
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _apply_cite_map(text: str, cite_map: dict[int, str]) -> str:
    seen: list[int] = []
    seen_set: set[int] = set()
    for m in _CITE_RE.finditer(text):
        n = int(m.group(1))
        if n not in seen_set:
            seen.append(n)
            seen_set.add(n)
    renumber = {orig: idx + 1 for idx, orig in enumerate(seen)}

    def replace(m: re.Match) -> str:
        n = int(m.group(1))
        new_n = renumber.get(n, n)
        url = cite_map.get(n)
        return f"[[{new_n}]](<{url}>)" if url else f"[{new_n}]"

    text = _CITE_RE.sub(replace, text)
    return _CONSEC_CITE_RE.sub(r') \1', text)


def _build_digest_chunks(
    memory_text: str | None,
    cite_map: dict[int, str] | None = None,
) -> list[str]:
    if not memory_text:
        return []
    text = _apply_cite_map(memory_text, cite_map) if cite_map else memory_text

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sent in sentences:
        addition = len(sent) + (1 if current else 0)
        if current and current_len + addition > _CONTENT_LIMIT:
            chunks.append(" ".join(current))
            current, current_len = [sent], len(sent)
        else:
            current.append(sent)
            current_len += addition
    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if c.strip()]


async def post_digest_to_discord(
    webhook_url: str,
    session: aiohttp.ClientSession,
    *,
    memory_text: str | None = None,
    cite_map: dict[int, str] | None = None,
) -> None:
    if not memory_text:
        return
    for chunk in _build_digest_chunks(memory_text, cite_map):
        await post_text_to_discord(webhook_url, chunk, session)


async def post_to_discord(
    webhook_url: str,
    entry: FeedEntry,
    session: aiohttp.ClientSession,
    fetch_og: bool = True,
    color: int | None = None,
) -> None:
    embed = {
        "title": entry.title,
        "url": entry.link or None,
        "color": color if color is not None else 0x5865F2,
    }
    if entry.description:
        embed["description"] = entry.description
    if entry.feed_title:
        embed["footer"] = {"text": entry.feed_title}

    og_image_url: str | None = entry.image
    if not og_image_url and fetch_og and entry.link:
        og_image_url = await _fetch_og_image(entry.link, session)

    image_bytes: bytes | None = None
    if og_image_url:
        raw = await _fetch_image(og_image_url, session)
        if raw is not None:
            image_bytes = _optimize_image(raw)

    if image_bytes is not None:
        embed["image"] = {"url": "attachment://og_image.webp"}
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({"embeds": [embed]}), content_type="application/json")
        form.add_field("files[0]", image_bytes, filename="og_image.webp", content_type="image/webp")
        post_kwargs: dict = {"data": form}
    else:
        if og_image_url:
            embed["image"] = {"url": og_image_url}
        post_kwargs = {"data": json.dumps({"embeds": [embed]}).encode(), "headers": {"Content-Type": "application/json"}}

    log.debug("Posting embed to Discord: %s", embed.get("title", ""))
    try:
        async with session.post(webhook_url, **post_kwargs) as resp:
            log.debug("Discord response: %s", resp.status)
            if resp.status not in (200, 204):
                log.warning("Unexpected Discord response: %s", resp.status)
            if resp.status >= 400:
                body = await resp.text()
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=body,
                )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise
