# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

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


async def _post_json(
    session: aiohttp.ClientSession,
    webhook_url: str,
    payload: dict,
) -> None:
    """POST a JSON payload to a webhook; logs and re-raises on HTTP/connection errors."""
    data = json.dumps(payload).encode()
    try:
        await _post_webhook(
            session,
            webhook_url,
            lambda: {"data": data, "headers": {"Content-Type": "application/json"}},
        )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


async def post_text_to_discord(
    webhook_url: str,
    text: str,
    session: aiohttp.ClientSession,
    *,
    wrap: bool = True,
) -> None:
    text = _suppress_embeds(text)
    text = "​\n" + text
    if wrap:
        text = _wrap_text(text)
    if len(text) > 2000:
        text = text[:1997] + "…"
    log.debug("Posting text to Discord (%d chars)", len(text))
    await _post_json(session, webhook_url, {"content": text})


_CONTENT_LIMIT = 2000
_LINE_WIDTH = 120
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_LINK_RE = re.compile(r"\[+[^\]]*\]\(<[^>]*>\)\]*")
_MASKED_LINK_RE = re.compile(r"(\[[^\]]*\])\((https?://[^)\s]+)\)")
_WRAPPED_URL_RE = re.compile(r"<https?://[^>\s]+>")
_BARE_URL_RE = re.compile(r"https?://[^\s<>]+")
_TRAILING_PUNCT = ".,;:!?'\""


def _wrap_bare_url(m: re.Match) -> str:
    url = m.group(0)
    trailing = ""
    while url:
        c = url[-1]
        if c in _TRAILING_PUNCT:
            pass
        elif c == ")" and url.count("(") < url.count(")"):
            pass
        elif c == "]" and url.count("[") < url.count("]"):
            pass
        else:
            break
        trailing = c + trailing
        url = url[:-1]
    return f"<{url}>{trailing}" if url else m.group(0)


def _suppress_embeds(text: str) -> str:
    """Wrap unwrapped URLs in ``<...>`` so Discord suppresses link previews.

    Already-wrapped forms (``<url>``, ``[text](<url>)``) are left alone;
    unwrapped masked links (``[text](url)``) get their inner URL wrapped;
    bare URLs get wrapped, sparing trailing punctuation likely outside the URL.
    """
    text = _MASKED_LINK_RE.sub(r"\1(<\2>)", text)
    stash: list[str] = []

    def _hold(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1:04d}\x00"

    text = _WRAPPED_URL_RE.sub(_hold, text)
    text = _BARE_URL_RE.sub(_wrap_bare_url, text)
    for i, original in enumerate(stash):
        text = text.replace(f"\x00{i:04d}\x00", original)
    return text


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
                links.append(f"[[{cit.source}]({cit.url})]" if cit.url else f"[{cit.source}]")
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


_EMBED_IMAGE_CAP = 4  # Discord merges up to 4 embeds sharing the same `url`.


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

    embeds = [embed]
    if not skip_image:
        imgs: list[str] = []
        seen: set[str] = set()
        sources = entry.images if entry.images else ([entry.image] if entry.image else [])
        for u in sources:
            if not isinstance(u, str):
                continue
            s = u.strip()
            if not s or s in seen:
                continue
            # Discord rejects the whole embed (HTTP 400) if image.url isn't a
            # well-formed absolute URL; drop it so we degrade to a no-image embed
            # rather than failing the entire post.
            if not s.startswith(("http://", "https://")):
                log.warning("Dropping non-absolute embed image URL: %s", s)
                continue
            seen.add(s)
            imgs.append(s)
            if len(imgs) >= _EMBED_IMAGE_CAP:
                break
        if imgs:
            embed["image"] = {"url": imgs[0]}
            # Merging extra images requires a shared, non-empty `url` on every embed.
            if len(imgs) > 1 and entry.url:
                embeds.extend({"url": entry.url, "image": {"url": u}} for u in imgs[1:])

    log.debug("Posting embed to Discord: %s", embed.get("title", ""))
    await _post_json(session, webhook_url, {"embeds": embeds})


async def _push_each(items: list[Item], post_one) -> set[str]:
    """Post items oldest-first with a rate-limit sleep between posts.

    A failed item is logged and collected into the returned failed set (by url)
    without aborting the rest of the batch.
    """
    failed: set[str] = set()
    ordered = sorted(items, key=lambda e: e.published or _FAR_FUTURE)
    for i, item in enumerate(ordered):
        try:
            await post_one(item)
            log.debug("[%s] Posted: %s", item.source, item.title[:80])
            if i < len(ordered) - 1:
                await asyncio.sleep(2)
        except Exception:
            log.error("Skipping entry %s due to post failure", item.id)
            if item.url:
                failed.add(item.url)
    return failed


class DiscordEmbedTarget(Target):
    """Posts each item as a Discord embed.

    `Item.meta["skip_image"]` suppresses the embed image even when `Item.image`
    is set; `Item.meta["color"]` overrides the default embed color.
    """

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        webhook = get_discord_cfg(cfg).get("webhook", "")

        async def _post_one(item: Item) -> None:
            await post_to_discord(
                webhook,
                item,
                session,
                skip_image=bool(item.meta.get("skip_image")),
                color=item.meta.get("color"),
            )

        return await _push_each(ctx.items, _post_one)


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
                log.debug("[%s] Posted text (%d chars)", item.source, len(item.body))
            except Exception:
                log.error("Skipping item %s due to post failure", item.id)
                if item.url:
                    failed.add(item.url)
        return failed


class DiscordMarkdownTarget(Target):
    """Posts each item as a markdown-formatted Discord message (## heading + body)."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        discord_cfg = get_discord_cfg(cfg)
        webhook = discord_cfg.get("webhook", "")
        wrap = discord_cfg.get("wrap", True)

        async def _post_one(item: Item) -> None:
            title_part = f"[{item.title}]({item.url})" if item.url else item.title
            source_part = f" [{item.source}]" if item.source else ""
            header = f"## {title_part}{source_part}"
            text = f"{header}\n{item.body}" if item.body else header
            await post_text_to_discord(webhook, text, session, wrap=wrap)

        return await _push_each(ctx.items, _post_one)


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
