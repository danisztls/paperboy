import asyncio
import io
import json
import logging
import re

import aiohttp
from bs4 import BeautifulSoup

from config import _get_discord_cfg
from pipeline import Item, PushContext, Target

log = logging.getLogger(__name__)

_BOT_DETECTION_THRESHOLD = 2048
_OG_FETCH_DELAY = 2.0


async def _scrape_image_once(url: str, session: aiohttp.ClientSession) -> tuple[str | None, bool]:
    """Single attempt. Returns (image_url, bot_detected)."""
    if not url:
        return None, False
    try:
        async with session.get(url) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            chunk = await resp.content.read(32768)
        if status >= 400:
            log.debug("image scrape failed for %s: HTTP %d", url, status)
            return None, False
        if len(chunk) < _BOT_DETECTION_THRESHOLD:
            log.debug("image scrape: got %d bytes (bot-detection?) from %s", len(chunk), url)
            return None, True
        text = chunk.decode("utf-8", errors="replace")
        log.debug("image scrape: fetched %d bytes (status=%d, ct=%s) from %s", len(chunk), status, content_type, url)
        meta = BeautifulSoup(text, "html.parser").find("meta", property="og:image")
        image_url = meta.get("content") if meta else None
        if image_url:
            log.debug("image scrape: found %s", image_url)
        else:
            log.debug("image scrape: no og:image tag found in first %d bytes of %s", len(chunk), url)
        return image_url, False
    except Exception as exc:
        log.debug("image scrape: exception fetching %s: %s: %s", url, type(exc).__name__, exc)
        return None, False


async def _scrape_image(url: str, session: aiohttp.ClientSession) -> str | None:
    """Fetch og:image URL from article HTML, with one bot-detection retry."""
    image_url, bot_detected = await _scrape_image_once(url, session)
    if bot_detected:
        log.debug("image scrape: bot-detected, retrying after %.0f s for %s", _OG_FETCH_DELAY, url)
        await asyncio.sleep(_OG_FETCH_DELAY)
        image_url, _ = await _scrape_image_once(url, session)
    return image_url

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


async def _post_webhook(
    session: aiohttp.ClientSession,
    url: str,
    **kwargs,
) -> None:
    """POST to a Discord webhook, retrying once on 429 rate-limit."""
    for attempt in range(2):
        async with session.post(url, **kwargs) as resp:
            if resp.status == 429 and attempt == 0:
                retry_after = min(float(resp.headers.get("Retry-After", "5")), 60.0)
                log.warning("Discord rate limited, retrying after %.1f s", retry_after)
                await asyncio.sleep(retry_after)
                continue
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
            return


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
        await _post_webhook(
            session, webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


_CONTENT_LIMIT = 2000
_CITE_RE = re.compile(r'\[(\d+)\]')
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _apply_cite_map(text: str, cite_map: dict[int, tuple[str, str | None]]) -> str:
    def replace(m: re.Match) -> str:
        item = cite_map.get(int(m.group(1)))
        if item is None:
            return m.group(0)
        name, url = item
        return f"[[{name}]](<{url}>)" if url else f"[{name}]"

    return _CITE_RE.sub(replace, text)


def _build_digest_chunks(
    memory_text: str | None,
    cite_map: dict[int, tuple[str, str | None]] | None = None,
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
    cite_map: dict[int, tuple[str, str | None]] | None = None,
) -> None:
    if not memory_text:
        return
    chunks = _build_digest_chunks(memory_text, cite_map)
    for i, chunk in enumerate(chunks):
        if i:
            await asyncio.sleep(1)
        await post_text_to_discord(webhook_url, chunk, session)


async def post_to_discord(
    webhook_url: str,
    entry: Item,
    session: aiohttp.ClientSession,
    fetch_image: bool = True,
    download_image: bool = False,
    color: int | None = None,
) -> None:
    embed = {
        "title": entry.title,
        "url": entry.url or None,
        "color": color if color is not None else 0x5865F2,
    }
    if entry.body:
        embed["description"] = entry.body
    if entry.source:
        embed["footer"] = {"text": entry.source}

    image_url: str | None = None
    if fetch_image:
        image_url = entry.image
        if not image_url and entry.url:
            image_url = await _scrape_image(entry.url, session)

    image_bytes: bytes | None = None
    if image_url and download_image:
        raw = await _fetch_image(image_url, session)
        if raw is not None:
            image_bytes = _optimize_image(raw)

    log.debug("Posting embed to Discord: %s", embed.get("title", ""))
    try:
        def _build_kwargs() -> dict:
            if image_bytes is not None:
                embed["image"] = {"url": "attachment://image.webp"}
                form = aiohttp.FormData()
                form.add_field("payload_json", json.dumps({"embeds": [embed]}), content_type="application/json")
                form.add_field("files[0]", image_bytes, filename="image.webp", content_type="image/webp")
                return {"data": form}
            if image_url:
                embed["image"] = {"url": image_url}
            return {"data": json.dumps({"embeds": [embed]}).encode(), "headers": {"Content-Type": "application/json"}}

        for attempt in range(2):
            async with session.post(webhook_url, **_build_kwargs()) as resp:
                if resp.status == 429 and attempt == 0:
                    retry_after = min(float(resp.headers.get("Retry-After", "5")), 60.0)
                    log.warning("Discord rate limited, retrying after %.1f s", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
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
                return
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


class DiscordEmbedTarget(Target):
    """Posts each item as a Discord embed."""

    def __init__(
        self,
        *,
        fetch_image: bool = True,
        color: int | None = None,
    ) -> None:
        self._fetch_image = fetch_image
        self._color = color

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = _get_discord_cfg(cfg).get("webhook", "")
        failed: set[str] = set()
        items = sorted(ctx.items, key=lambda e: e.published or _FAR_FUTURE)
        for i, item in enumerate(items):
            color = item.meta.get("color") or self._color
            download_image = item.meta.get("download_image", False)
            try:
                await post_to_discord(
                    webhook, item, session,
                    fetch_image=self._fetch_image,
                    download_image=download_image,
                    color=color,
                )
                log.info("[%s] Posted: %s", item.source, item.title[:80])
                if i < len(items) - 1:
                    await asyncio.sleep(2)
            except Exception:
                log.error("Skipping entry %s due to post failure", item.id)
                if item.url:
                    failed.add(item.url)
        return failed


class DiscordTextTarget(Target):
    """Posts each item's body as a plain Discord text message."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = _get_discord_cfg(cfg).get("webhook", "")
        failed: set[str] = set()
        for item in ctx.items:
            if not item.body:
                continue
            try:
                await post_text_to_discord(webhook, item.body, session)
                log.info("[%s] Posted text (%d chars)", item.source, len(item.body))
            except Exception:
                log.error("Skipping item %s due to post failure", item.id)
                if item.url:
                    failed.add(item.url)
        return failed


class DiscordDigestTarget(Target):
    """Posts the memory briefing as chunked Discord text messages."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = _get_discord_cfg(cfg).get("webhook", "")
        if not ctx.memory:
            return set()
        try:
            await post_digest_to_discord(webhook, session, memory_text=ctx.memory, cite_map=ctx.cite_map)
        except Exception:
            log.error("Failed to post digest")
            raise
        return set()


from datetime import datetime, timezone
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)
