#!/usr/bin/env python3
"""Summarization model benchmark: compares models on a fixed set of URLs."""

import asyncio
import pathlib
import time
from datetime import datetime

import aiohttp

from llm import get_adapter
from summarize import (
    _YOUTUBE_RE,
    _fetch_article_content,
    _fetch_youtube_data,
    summarize_entry,
    summarize_transcript,
)

URLS = [
    "https://www.youtube.com/watch?v=h9dgeM_KuB8",
    "https://www.anthropic.com/research/tracing-thoughts-language-model",
    "https://www.youtube.com/watch?v=uFxi7YrbNJQ",
    "https://www.vaticannews.va/pt/vaticano/news/2026-05/santa-se-chica-arellano-alimentacao-agricultura-fao-fida-pma.html",
]

MODELS = [
    ("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6 [reference]"),
    ("openai", "gpt-5.4-nano", "GPT-5.4 Nano"),
    ("gemini", "gemini-3.1-flash-lite-preview", "Gemini 3.1 Flash Lite Preview"),
    ("deepseek", "deepseek-v4-flash", "DeepSeek V4 Flash"),
]

_SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
}


async def fetch_content(url: str, session: aiohttp.ClientSession) -> tuple[str, str, str]:
    """Return (url, title, content) for a URL. title may be empty for articles."""
    if _YOUTUBE_RE.match(url):
        result = await _fetch_youtube_data(url, session)
        if not result:
            return url, "", ""
        title, transcript = result
        return url, title, transcript
    content = await _fetch_article_content(url, session)
    return url, "", content or ""


async def run_model(
    provider: str,
    model: str,
    label: str,
    url: str,
    title: str,
    content: str,
    is_youtube: bool,
) -> tuple[str, str, float, str | None]:
    """Return (label, url, elapsed_seconds, summary)."""
    adapter = get_adapter(provider)
    start = time.monotonic()
    if is_youtube:
        summary = await summarize_transcript(title, content, adapter, model=model)
    else:
        summary = await summarize_entry(title or url, content, adapter, model=model)
    elapsed = time.monotonic() - start
    return label, url, elapsed, summary


def _divider(char: str = "─", width: int = 72) -> str:
    return char * width


def _format_result(label: str, elapsed: float, summary: str | None) -> str:
    lines = [f"\n  [{label}] ({elapsed:.1f}s)", "  " + _divider("·", 68)]
    if summary:
        lines.extend(f"  {line}" for line in summary.splitlines())
    else:
        lines.append("  (no output)")
    return "\n".join(lines)


def _print_result(label: str, elapsed: float, summary: str | None) -> None:
    print(_format_result(label, elapsed, summary))


async def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = pathlib.Path("benchmark_results")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"benchmark_{ts}.txt"

    buf: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        buf.append(line)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60),
        headers=_SESSION_HEADERS,
    ) as session:
        emit("Fetching content from all URLs…")
        fetch_tasks = [fetch_content(url, session) for url in URLS]
        contents = await asyncio.gather(*fetch_tasks)

    emit(f"\nFetched {len(contents)} items. Running {len(MODELS)} models on each.\n")
    emit(_divider("═"))

    for url, title, content in contents:
        is_youtube = bool(_YOUTUBE_RE.match(url))
        display = title if title else url
        kind = "YouTube" if is_youtube else "Article"
        emit(f"\n{kind}: {display}")
        emit(f"URL: {url}")
        emit(_divider())

        if not content:
            emit("  ERROR: could not fetch content — skipping")
            continue

        model_tasks = [
            run_model(provider, model, label, url, title, content, is_youtube)
            for provider, model, label in MODELS
        ]
        results = await asyncio.gather(*model_tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                line = f"\n  ERROR: {result}"
                print(line)
                buf.append(line)
            else:
                label, _, elapsed, summary = result
                block = _format_result(label, elapsed, summary)
                print(block)
                buf.append(block)

        emit()

    emit(_divider("═"))
    emit("Benchmark complete.")

    out_path.write_text("\n".join(buf))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
