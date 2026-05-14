"""Transcript/article fetch and LLM summarization for YouTube and arbitrary URLs."""

import asyncio
import logging
import re
import sys

import aiohttp
import trafilatura
import yt_dlp
from bs4 import BeautifulSoup

from constants import USER_AGENT
from providers.llm.base import LLMAdapter

log = logging.getLogger(__name__)


async def summarize_entry(
    title: str,
    description: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    language: str = "EN-US",
    instructions: str | None = None,
    reasoning: bool | dict = False,
    trace: dict | None = None,
) -> str | None:
    base_instructions = (
        f"You are a precise, concise summarizer. Write in {language}. "
        "Given the title and description of a news article or feed entry, write a brief summary "
        "covering the main point and key details. No filler phrases. "
        "Keep the summary under 1024 characters."
    )
    combined = f"{base_instructions} {instructions}" if instructions else base_instructions
    input_text = f"Title: {title}\n\nDescription:\n{description}"
    if trace is not None:
        trace["instructions"] = combined
        trace["input"] = input_text
    log.info("Summarizing entry (model=%s): %s", model, title[:80])
    resp = await adapter.complete(
        input_text, model=model, instructions=combined, reasoning=reasoning
    )
    if resp is None:
        if trace is not None:
            trace["output"] = None
        return None
    if trace is not None:
        trace["output"] = resp.text
        trace["input_tokens"] = resp.input_tokens
        trace["output_tokens"] = resp.output_tokens
        trace["latency_s"] = resp.latency_s
        trace["model_used"] = resp.model
        if resp.reasoning:
            trace["reasoning"] = resp.reasoning
    return resp.text


async def summarize_transcript(
    title: str,
    transcript: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    language: str = "EN-US",
) -> str | None:
    instructions = (
        f"You are a precise, concise summarizer. Write in {language}. "
        "Given the title and transcript of a YouTube video, write a clear summary covering the main topics and key takeaways. "
        "Use a few short paragraphs. No filler phrases or meta-commentary about the summarization process."
    )
    log.info(
        "Summarizing transcript (model=%s, language=%s, %d chars)", model, language, len(transcript)
    )
    resp = await adapter.complete(
        f"Title: {title}\n\nTranscript:\n{transcript[:12000]}",
        model=model,
        instructions=instructions,
    )
    return resp.text if resp else None


_VTT_CUE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")
_VTT_TAGS_RE = re.compile(r"<[^>]+>")
_VTT_META_RE = re.compile(r"^(WEBVTT|Kind:|Language:)")

_YOUTUBE_RE = re.compile(r"https?://(?:[a-z0-9-]+\.)*youtube\.com(?:\.[a-z]{2,})?/", re.IGNORECASE)


def _parse_vtt(content: str) -> list[tuple[float, str]]:
    """Extract (start_seconds, text) tuples from a WebVTT subtitle file.

    YouTube auto-captions emit overlapping rolling cues; consecutive
    identical lines are deduplicated within each cue block.
    """
    cues: list[tuple[float, str]] = []
    current_start: float | None = None
    current_lines: list[str] = []

    def _flush():
        if current_start is None or not current_lines:
            return
        deduped: list[str] = []
        for ln in current_lines:
            if not deduped or ln != deduped[-1]:
                deduped.append(ln)
        text = " ".join(deduped)
        if text:
            cues.append((current_start, text))

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            _flush()
            current_start = None
            current_lines = []
            continue
        if _VTT_META_RE.match(line) or line.isdigit():
            continue
        m = _VTT_CUE_RE.match(line)
        if m:
            _flush()
            h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            current_start = h * 3600 + mn * 60 + s + ms / 1000
            current_lines = []
            continue
        cleaned = _VTT_TAGS_RE.sub("", line).strip()
        if cleaned:
            current_lines.append(cleaned)

    _flush()
    return cues


def _apply_sponsorblock(
    cues: list[tuple[float, str]], segments: list[dict]
) -> list[tuple[float, str]]:
    """Remove cues whose start time falls inside any SponsorBlock segment."""
    if not segments:
        return cues
    blocked = [(seg["segment"][0], seg["segment"][1]) for seg in segments]
    return [(t, text) for t, text in cues if not any(start <= t < end for start, end in blocked)]


def _cues_to_text(cues: list[tuple[float, str]]) -> str:
    """Deduplicate consecutive identical cue texts and join."""
    deduped: list[str] = []
    for _, text in cues:
        if not deduped or text != deduped[-1]:
            deduped.append(text)
    return " ".join(deduped)


async def _fetch_sponsorblock(video_id: str, session: aiohttp.ClientSession) -> list[dict]:
    categories = '["sponsor","selfpromo","interaction","intro","outro"]'
    url = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}&categories={categories}"
    try:
        async with session.get(url) as resp:
            if resp.status == 404:
                return []
            resp.raise_for_status()
            return await resp.json()
    except Exception as exc:
        log.warning("SponsorBlock lookup failed: %s", exc)
        return []


async def _fetch_youtube_data(url: str, session: aiohttp.ClientSession) -> tuple[str, str] | None:
    """Return (title, transcript) for a YouTube URL, or None on failure."""

    def _extract():
        with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)

    log.info("Fetching video info: %s", url)
    try:
        info = await asyncio.to_thread(_extract)
    except Exception as exc:
        log.warning("yt-dlp failed for %s: %s", url, exc)
        return None

    title = info.get("title", "")
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    lang_track = None
    for src in (subs, auto):
        for lang in ("en", "en-orig", *src.keys()):
            if lang in src:
                lang_track = src[lang]
                break
        if lang_track:
            break

    if not lang_track:
        log.warning("No subtitles or captions found for %s", url)
        return None

    vtt_entry = next((e for e in lang_track if e.get("ext") == "vtt"), lang_track[0])
    sub_url = vtt_entry.get("url")
    if not sub_url:
        log.warning("No subtitle URL found for %s", url)
        return None

    video_id = info.get("id", "")
    log.info("Fetching captions for %r", title)

    try:

        async def _fetch_vtt() -> str:
            async with session.get(sub_url) as resp:
                return await resp.text()

        vtt_content, sb_segments = await asyncio.gather(
            _fetch_vtt(),
            _fetch_sponsorblock(video_id, session),
        )
    except aiohttp.ClientError as exc:
        log.warning("Failed to fetch captions for %s: %s", url, exc)
        return None

    cues = _parse_vtt(vtt_content)
    if not cues:
        log.warning("No text extracted from captions for %s", url)
        return None

    if sb_segments:
        before = len(cues)
        cues = _apply_sponsorblock(cues, sb_segments)
        log.info(
            "SponsorBlock removed %d/%d cues (%d segments)",
            before - len(cues),
            before,
            len(sb_segments),
        )
    else:
        log.info("No SponsorBlock segments found")

    transcript = _cues_to_text(cues)
    if not transcript:
        log.warning("No text remained after SponsorBlock filtering for %s", url)
        return None

    log.info("Transcript length: %d chars", len(transcript))
    return title, transcript


def extract_og_image(html: str) -> str | None:
    """Return the og:image URL from raw HTML, or None if absent."""
    meta = BeautifulSoup(html, "html.parser").find("meta", property="og:image")
    return meta.get("content") if meta else None


async def _fetch_article(
    url: str, session: aiohttp.ClientSession, *, with_title: bool = False
) -> tuple[str, str, str | None] | None:
    """Extract (title, body, og_image) from a URL using trafilatura.

    `title` is `""` unless `with_title=True`. Returns None on fetch or extraction failure.
    """
    try:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            html = await resp.text()
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None

    def _extract():
        doc = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
            output_format="markdown",
        )
        if not doc:
            return None
        title = ""
        if with_title:
            meta = trafilatura.extract_metadata(html, default_url=url)
            title = (meta.title if meta else "") or ""
        return title, doc, extract_og_image(html)

    result = await asyncio.to_thread(_extract)
    if not result:
        log.warning("trafilatura extracted no content from %s", url)
        return None

    log.info("Article extracted: %d chars from %s", len(result[1]), url)
    return result


async def fetch_item_content(
    url: str, session: aiohttp.ClientSession
) -> tuple[str, str | None] | None:
    """Return (content, og_image) for a feed item URL, or None.

    YouTube URLs: returns the video transcript via yt-dlp + SponsorBlock filtering (no og:image).
    Other URLs: returns article text extracted by trafilatura (markdown format) plus the og:image
    URL scraped from the same HTML, if present.
    """
    if _YOUTUBE_RE.match(url):
        result = await _fetch_youtube_data(url, session)
        return (result[1], None) if result else None
    article = await _fetch_article(url, session)
    if not article:
        return None
    _title, body, og = article
    return body, og


async def run_summarize(
    url: str, adapter: LLMAdapter, model: str | None, language: str = "EN-US"
) -> None:
    is_youtube = bool(_YOUTUBE_RE.match(url))

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": USER_AGENT},
    ) as session:
        if is_youtube:
            result = await _fetch_youtube_data(url, session)
            if not result:
                log.error("Could not fetch transcript for %s", url)
                sys.exit(1)
            title, transcript = result
            summary = await summarize_transcript(
                title, transcript, adapter, model=model, language=language
            )
        else:
            result = await _fetch_article(url, session)
            if not result:
                log.error("Could not fetch article content for %s", url)
                sys.exit(1)
            _title, content, _og = result
            summary = await summarize_entry(url, content, adapter, model=model, language=language)

    if summary:
        print(summary)
    else:
        log.error("Summarization failed")
        sys.exit(1)
