import asyncio
import json
import logging
import re
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime

import aiohttp

from config import get_discord_cfg
from pipeline import Citation, Item, MemoryParagraph, PushContext, Target

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
    *,
    wrap: bool = True,
) -> None:
    text = "​\n" + text
    if wrap:
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
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
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


def _render_paragraph(para: MemoryParagraph, cite_map: dict[int, Citation] | None) -> str:
    text = para.text.rstrip()
    if para.citations and cite_map:
        links = []
        for id_ in para.citations:
            cit = cite_map.get(id_)
            if cit:
                links.append(f"[[{cit.source}](<{cit.url}>)]" if cit.url else f"[{cit.source}]")
        if links:
            text += " " + " ".join(links)
    return text


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


def _split_themes(memory: list[MemoryParagraph]) -> list[list[MemoryParagraph]]:
    """Group paragraphs by section. A new theme starts at each paragraph with `section` set;
    paragraphs without `section` belong to the current theme (or a leading sectionless group)."""
    themes: list[list[MemoryParagraph]] = []
    current: list[MemoryParagraph] = []
    for para in memory:
        if para.section and current:
            themes.append(current)
            current = []
        current.append(para)
    if current:
        themes.append(current)
    return themes


def _build_digest_chunks(
    memory: list[MemoryParagraph] | None,
    cite_map: dict[int, Citation] | None = None,
) -> list[str]:
    if not memory:
        return []

    # Pre-split any rendered paragraph that's too long into sentence-packed sub-chunks.
    # Section headings are glued to their first paragraph so they can never be orphaned.
    units: list[str] = []
    for para in memory:
        if not para.text.strip():
            continue
        rendered = _render_paragraph(para, cite_map)
        if para.section:
            rendered = f"## {para.section}\n\n{rendered}"
        if len(rendered) <= _CONTENT_LIMIT:
            units.append(rendered)
            continue
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(rendered) if s.strip()]
        units.extend(_pack(sentences, " ", _CONTENT_LIMIT))

    chunks = _pack(units, "\n\n", _CONTENT_LIMIT)
    return [c for c in chunks if c.strip()]


async def post_digest_to_discord(
    webhook_url: str,
    session: aiohttp.ClientSession,
    *,
    memory: list[MemoryParagraph] | None = None,
    cite_map: dict[int, Citation] | None = None,
) -> None:
    if not memory:
        return
    first = True
    for theme in _split_themes(memory):
        for chunk in _build_digest_chunks(theme, cite_map):
            if not first:
                await asyncio.sleep(1)
            await post_text_to_discord(webhook_url, chunk, session)
            first = False


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
        discord_cfg = get_discord_cfg(cfg)
        webhook = discord_cfg.get("webhook", "")
        wrap = discord_cfg.get("wrap", True)
        failed: set[str] = set()
        for item in ctx.items:
            if not item.body:
                continue
            try:
                await post_text_to_discord(webhook, item.body, session, wrap=wrap)
                log.info("[%s] Posted text (%d chars)", item.source, len(item.body))
            except Exception:
                log.error("Skipping item %s due to post failure", item.id)
                if item.url:
                    failed.add(item.url)
        return failed


class DiscordMarkdownTarget(Target):
    """Posts each item as a markdown-formatted Discord message (### heading + body)."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        discord_cfg = get_discord_cfg(cfg)
        webhook = discord_cfg.get("webhook", "")
        wrap = discord_cfg.get("wrap", True)
        failed: set[str] = set()
        items = sorted(ctx.items, key=lambda e: e.published or _FAR_FUTURE)
        for i, item in enumerate(items):
            title_part = f"[{item.title}](<{item.url}>)" if item.url else item.title
            source_part = f" [{item.source}]" if item.source else ""
            header = f"### {title_part}{source_part}"
            body = item.body or ""
            text = f"{header}\n{body}" if body else header
            try:
                await post_text_to_discord(webhook, text, session, wrap=wrap)
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
            await post_digest_to_discord(webhook, session, memory=ctx.memory, cite_map=ctx.cite_map)
        except Exception:
            log.error("Failed to post digest")
            raise
        return set()


_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
