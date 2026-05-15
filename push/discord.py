import asyncio
import json
import logging
import re
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime

import aiohttp

from config import get_discord_cfg
from pipeline import Citation, Item, PushContext, Target

log = logging.getLogger(__name__)


async def _post_webhook(
    session: aiohttp.ClientSession,
    url: str,
    build_kwargs: Callable[[], dict],
) -> None:
    """POST to a Discord webhook, retrying once on 429 rate-limit.

    `build_kwargs` is called per attempt so callers using one-shot payloads
    (e.g. aiohttp.FormData) can produce a fresh instance on retry.
    """
    for attempt in range(2):
        async with session.post(url, **build_kwargs()) as resp:
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
    text = _wrap_text(text)
    if len(text) > 2000:
        text = text[:1997] + "…"
    payload = json.dumps({"content": text}).encode()
    log.debug("Posting text to Discord (%d chars)", len(text))
    try:
        await _post_webhook(
            session,
            webhook_url,
            lambda: {"data": payload, "headers": {"Content-Type": "application/json"}},
        )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


_CONTENT_LIMIT = 2000
_LINE_WIDTH = 120
_CITE_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
_LINK_RE = re.compile(r"\[+[^\]]*\]\(<[^>]*>\)\]*")


def _protect_links(line: str) -> tuple[str, list[str]]:
    """Replace masked Discord links with placeholders so textwrap won't break them."""
    originals: list[str] = []

    def _sub(m: re.Match) -> str:
        originals.append(m.group(0))
        return f"\x00{len(originals) - 1:04d}\x00"

    return _LINK_RE.sub(_sub, line), originals


def _wrap_text(text: str) -> str:
    lines = text.split("\n")
    result = []
    for line in lines:
        if len(line) <= _LINE_WIDTH or line.startswith("#"):
            result.append(line)
        else:
            protected, originals = _protect_links(line)
            wrapped = textwrap.fill(
                protected, width=_LINE_WIDTH, break_long_words=False, break_on_hyphens=False
            )
            for i, original in enumerate(originals):
                wrapped = wrapped.replace(f"\x00{i:04d}\x00", original)
            result.append(wrapped)
    return "\n".join(result)


def _apply_cite_map(text: str, cite_map: dict[int, Citation]) -> str:
    def replace(m: re.Match) -> str:
        item = cite_map.get(int(m.group(1)))
        if item is None:
            return m.group(0)
        return f"[[{item.source}](<{item.url}>)]" if item.url else f"[{item.source}]"

    return _CITE_RE.sub(replace, text)


def _pack(units: list[str], sep: str, limit: int) -> list[str]:
    """Greedy pack `units` into chunks no longer than `limit`, joined by `sep`."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        addition = len(unit) + (len(sep) if current else 0)
        if current and current_len + addition > limit:
            chunks.append(sep.join(current))
            current, current_len = [unit], len(unit)
        else:
            current.append(unit)
            current_len += addition
    if current:
        chunks.append(sep.join(current))
    return chunks


def _build_digest_chunks(
    memory_text: str | None,
    cite_map: dict[int, Citation] | None = None,
) -> list[str]:
    if not memory_text:
        return []
    text = _apply_cite_map(memory_text, cite_map) if cite_map else memory_text

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text.strip()) if p.strip()]

    # Pre-split any paragraph that's too long on its own into sentence-packed sub-chunks.
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= _CONTENT_LIMIT:
            units.append(para)
            continue
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
        units.extend(_pack(sentences, " ", _CONTENT_LIMIT))

    chunks = _pack(units, "\n\n", _CONTENT_LIMIT)
    return [c for c in chunks if c.strip()]


async def post_digest_to_discord(
    webhook_url: str,
    session: aiohttp.ClientSession,
    *,
    memory_text: str | None = None,
    cite_map: dict[int, Citation] | None = None,
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
    skip_image: bool = False,
    color: int | None = None,
) -> None:
    embed = {
        "title": entry.title,
        "url": entry.url or None,
        "color": color if color is not None else 0x5865F2,
    }
    if entry.body:
        embed["description"] = _wrap_text(entry.body)
    if entry.source:
        embed["footer"] = {"text": entry.source}

    if not skip_image and entry.image:
        embed["image"] = {"url": entry.image}

    log.debug("Posting embed to Discord: %s", embed.get("title", ""))
    payload = json.dumps({"embeds": [embed]}).encode()
    try:
        await _post_webhook(
            session,
            webhook_url,
            lambda: {"data": payload, "headers": {"Content-Type": "application/json"}},
        )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


class DiscordEmbedTarget(Target):
    """Posts each item as a Discord embed.

    `Item.meta["skip_image"]` suppresses the embed image even when `Item.image`
    is set; `Item.meta["color"]` overrides the default embed color.
    """

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = get_discord_cfg(cfg).get("webhook", "")
        failed: set[str] = set()
        items = sorted(ctx.items, key=lambda e: e.published or _FAR_FUTURE)
        for i, item in enumerate(items):
            try:
                await post_to_discord(
                    webhook,
                    item,
                    session,
                    skip_image=bool(item.meta.get("skip_image")),
                    color=item.meta.get("color"),
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
        webhook = get_discord_cfg(cfg).get("webhook", "")
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


class DiscordMarkdownTarget(Target):
    """Posts each item as a markdown-formatted Discord message (### heading + body)."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = get_discord_cfg(cfg).get("webhook", "")
        failed: set[str] = set()
        items = sorted(ctx.items, key=lambda e: e.published or _FAR_FUTURE)
        for i, item in enumerate(items):
            title_part = f"[{item.title}](<{item.url}>)" if item.url else item.title
            source_part = f" [{item.source}]" if item.source else ""
            header = f"### {title_part}{source_part}"
            body = item.body or ""
            text = f"{header}\n{body}" if body else header
            try:
                await post_text_to_discord(webhook, text, session)
                log.info("[%s] Posted: %s", item.source, item.title[:80])
                if i < len(items) - 1:
                    await asyncio.sleep(2)
            except Exception:
                log.error("Skipping entry %s due to post failure", item.id)
                if item.url:
                    failed.add(item.url)
        return failed


class DiscordDigestTarget(Target):
    """Posts the memory briefing as chunked Discord text messages."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = get_discord_cfg(cfg).get("webhook", "")
        if not ctx.memory:
            return set()
        try:
            await post_digest_to_discord(
                webhook, session, memory_text=ctx.memory, cite_map=ctx.cite_map
            )
        except Exception:
            log.error("Failed to post digest")
            raise
        return set()


_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
