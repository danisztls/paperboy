"""YouTube transcript fetch, SponsorBlock filtering, and LLM summarization."""

import asyncio
import logging
import re
import sys

import aiohttp

from llm import summarize_transcript

log = logging.getLogger(__name__)

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


async def run_summarize(url: str, api_key: str | None, model: str | None, language: str = "EN-US") -> None:
    if not _YOUTUBE_RE.match(url):
        log.error("--summarize only supports youtube.com URLs")
        sys.exit(1)

    try:
        import yt_dlp
    except ImportError:
        log.error("yt-dlp is not installed — run: uv sync")
        sys.exit(1)

    def _extract():
        with yt_dlp.YoutubeDL({'skip_download': True, 'quiet': True, 'no_warnings': True}) as ydl:
            return ydl.extract_info(url, download=False)

    log.info("Fetching video info: %s", url)
    try:
        info = await asyncio.to_thread(_extract)
    except Exception as exc:
        log.error("yt-dlp failed: %s", exc)
        sys.exit(1)

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
        log.error("No subtitles or captions found for this video")
        sys.exit(1)

    vtt_entry = next((e for e in lang_track if e.get('ext') == 'vtt'), lang_track[0])
    sub_url = vtt_entry.get('url')
    if not sub_url:
        log.error("No subtitle URL found")
        sys.exit(1)

    video_id = info.get('id', '')
    log.info("Fetching captions for %r", title)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
    ) as session:
        async def _fetch_vtt() -> str:
            async with session.get(sub_url) as resp:
                return await resp.text()

        try:
            vtt_content, sb_segments = await asyncio.gather(
                _fetch_vtt(),
                _fetch_sponsorblock(video_id, session),
            )
        except aiohttp.ClientError as exc:
            log.error("Failed to fetch captions: %s", exc)
            sys.exit(1)

    cues = _parse_vtt(vtt_content)
    if not cues:
        log.error("No text could be extracted from captions")
        sys.exit(1)

    if sb_segments:
        before = len(cues)
        cues = _apply_sponsorblock(cues, sb_segments)
        log.info("SponsorBlock removed %d/%d cues (%d segments)", before - len(cues), before, len(sb_segments))
    else:
        log.info("No SponsorBlock segments found")

    transcript = _cues_to_text(cues)
    if not transcript:
        log.error("No text remained after SponsorBlock filtering")
        sys.exit(1)

    log.info("Transcript length: %d chars", len(transcript))
    summary = await summarize_transcript(title, transcript, api_key=api_key, model=model, language=language)
    if summary:
        print(summary)
    else:
        log.error("Summarization failed")
        sys.exit(1)
