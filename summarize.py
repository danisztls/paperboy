"""YouTube transcript fetch, SponsorBlock filtering, and LLM summarization."""

import asyncio
import logging
import re
import sys

import aiohttp
from openai import AsyncOpenAI

_DEFAULT_MODEL = "gpt-5.4-mini"

log = logging.getLogger(__name__)


async def summarize_entry(
    title: str,
    description: str,
    api_key: str | None = None,
    model: str | None = None,
    language: str = "EN-US",
    instructions: str | None = None,
) -> str | None:
    client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
    model = model or _DEFAULT_MODEL
    base_instructions = (
        f"You are a precise, concise summarizer. Write in {language}. "
        "Given the title and description of a news article or feed entry, write a brief summary "
        "covering the main point and key details. No filler phrases. "
        "Keep the summary under 1024 characters."
    )
    instructions = f"{base_instructions} {instructions}" if instructions else base_instructions
    log.info("Summarizing entry (model=%s): %s", model, title[:80])
    try:
        response = await client.responses.create(
            model=model,
            instructions=instructions,
            input=f"Title: {title}\n\nDescription:\n{description}",
        )
        return (response.output_text or "").strip() or None
    except Exception as exc:
        log.error("Summarize entry failed: %s", exc)
        return None


async def summarize_transcript(
    title: str,
    transcript: str,
    api_key: str | None = None,
    model: str | None = None,
    language: str = "EN-US",
) -> str | None:
    client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
    model = model or _DEFAULT_MODEL
    instructions = (
        f"You are a precise, concise summarizer. Write in {language}. "
        "Given the title and transcript of a YouTube video, write a clear summary covering the main topics and key takeaways. "
        "Use a few short paragraphs. No filler phrases or meta-commentary about the summarization process."
    )
    log.info("Summarizing transcript (model=%s, language=%s, %d chars)", model, language, len(transcript))
    try:
        response = await client.responses.create(
            model=model,
            instructions=instructions,
            input=f"Title: {title}\n\nTranscript:\n{transcript[:12000]}",
        )
        return (response.output_text or "").strip() or None
    except Exception as exc:
        log.error("Summarize failed: %s", exc)
        return None

_VTT_CUE_RE = re.compile(
    r'^(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->'
)
_VTT_TAGS_RE = re.compile(r'<[^>]+>')
_VTT_META_RE = re.compile(r'^(WEBVTT|Kind:|Language:)')

_YOUTUBE_RE = re.compile(r'https?://(?:[a-z0-9-]+\.)*youtube\.com(?:\.[a-z]{2,})?/', re.IGNORECASE)


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
        text = ' '.join(deduped)
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
        cleaned = _VTT_TAGS_RE.sub('', line).strip()
        if cleaned:
            current_lines.append(cleaned)

    _flush()
    return cues


def _apply_sponsorblock(cues: list[tuple[float, str]], segments: list[dict]) -> list[tuple[float, str]]:
    """Remove cues whose start time falls inside any SponsorBlock segment."""
    if not segments:
        return cues
    blocked = [(seg['segment'][0], seg['segment'][1]) for seg in segments]
    return [(t, text) for t, text in cues if not any(start <= t < end for start, end in blocked)]


def _cues_to_text(cues: list[tuple[float, str]]) -> str:
    """Deduplicate consecutive identical cue texts and join."""
    deduped: list[str] = []
    for _, text in cues:
        if not deduped or text != deduped[-1]:
            deduped.append(text)
    return ' '.join(deduped)


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
    try:
        import yt_dlp
    except ImportError:
        log.warning("yt-dlp is not installed — transcript fetch skipped")
        return None

    def _extract():
        with yt_dlp.YoutubeDL({'skip_download': True, 'quiet': True, 'no_warnings': True}) as ydl:
            return ydl.extract_info(url, download=False)

    log.info("Fetching video info: %s", url)
    try:
        info = await asyncio.to_thread(_extract)
    except Exception as exc:
        log.warning("yt-dlp failed for %s: %s", url, exc)
        return None

    title = info.get('title', '')
    subs = info.get('subtitles') or {}
    auto = info.get('automatic_captions') or {}

    lang_track = None
    for src in (subs, auto):
        for lang in ('en', 'en-orig', *src.keys()):
            if lang in src:
                lang_track = src[lang]
                break
        if lang_track:
            break

    if not lang_track:
        log.warning("No subtitles or captions found for %s", url)
        return None

    vtt_entry = next((e for e in lang_track if e.get('ext') == 'vtt'), lang_track[0])
    sub_url = vtt_entry.get('url')
    if not sub_url:
        log.warning("No subtitle URL found for %s", url)
        return None

    video_id = info.get('id', '')
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
        log.info("SponsorBlock removed %d/%d cues (%d segments)", before - len(cues), before, len(sb_segments))
    else:
        log.info("No SponsorBlock segments found")

    transcript = _cues_to_text(cues)
    if not transcript:
        log.warning("No text remained after SponsorBlock filtering for %s", url)
        return None

    log.info("Transcript length: %d chars", len(transcript))
    return title, transcript


async def _fetch_article_content(url: str, session: aiohttp.ClientSession) -> str | None:
    """Extract article text from a URL using trafilatura."""
    try:
        import trafilatura
    except ImportError:
        log.warning("trafilatura is not installed — article fetch skipped")
        return None

    try:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            html = await resp.text()
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None

    def _extract() -> str | None:
        doc = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
            output_format="markdown",
        )
        return doc

    result = await asyncio.to_thread(_extract)
    if not result:
        log.warning("trafilatura extracted no content from %s", url)
        return None

    log.info("Article extracted: %d chars from %s", len(result), url)
    return result


async def fetch_item_content(url: str, session: aiohttp.ClientSession) -> str | None:
    """Return fetchable text content for a feed item URL, or None.

    YouTube URLs: returns the video transcript via yt-dlp + SponsorBlock filtering.
    Other URLs: returns article text extracted by trafilatura (markdown format).
    Falls back to None when content cannot be fetched.
    """
    if _YOUTUBE_RE.match(url):
        result = await _fetch_youtube_data(url, session)
        return result[1] if result else None
    return await _fetch_article_content(url, session)


async def run_summarize(url: str, api_key: str | None, model: str | None, language: str = "EN-US") -> None:
    if not _YOUTUBE_RE.match(url):
        log.error("--summarize only supports youtube.com URLs")
        sys.exit(1)

    try:
        import yt_dlp  # noqa: F401 — check before opening session
    except ImportError:
        log.error("yt-dlp is not installed — run: uv sync")
        sys.exit(1)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
    ) as session:
        result = await _fetch_youtube_data(url, session)

    if not result:
        log.error("Could not fetch transcript for %s", url)
        sys.exit(1)

    title, transcript = result
    summary = await summarize_transcript(title, transcript, api_key=api_key, model=model, language=language)
    if summary:
        print(summary)
    else:
        log.error("Summarization failed")
        sys.exit(1)
