#!/usr/bin/env python3
"""Summarization model benchmark: compares models on a fixed set of URLs."""

import asyncio
import json
import pathlib
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import aiohttp
import yaml

from llm import get_adapter
from summarize import (
    _YOUTUBE_RE,
    _fetch_article_data,
    _fetch_youtube_data,
    summarize_entry,
    summarize_transcript,
)

_CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"

_SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
}


async def fetch_content(url: str, session: aiohttp.ClientSession) -> tuple[str, str, str]:
    """Return (url, title, content) for a URL."""
    if _YOUTUBE_RE.match(url):
        result = await _fetch_youtube_data(url, session)
        if not result:
            return url, "", ""
        title, transcript = result
        return url, title, transcript
    result = await _fetch_article_data(url, session)
    if not result:
        return url, "", ""
    title, content = result
    return url, title, content


async def run_model(
    provider: str,
    model: str,
    url: str,
    title: str,
    content: str,
    is_youtube: bool,
) -> tuple[str, str, str, float, str | None]:
    """Return (provider, model, url, elapsed_seconds, summary). Retries once on failure."""
    adapter = get_adapter(provider)
    start = time.monotonic()
    last_exc: BaseException | None = None
    for attempt in range(2):
        try:
            if is_youtube:
                summary = await summarize_transcript(title, content, adapter, model=model)
            else:
                summary = await summarize_entry(title or url, content, adapter, model=model)
            if summary is not None:
                return provider, model, url, time.monotonic() - start, summary
        except Exception as exc:
            last_exc = exc
        if attempt == 0:
            await asyncio.sleep(2)
    if last_exc:
        raise last_exc
    raise RuntimeError("model returned no output after retry (model may be unavailable)")


async def process_url(
    url: str,
    title: str,
    content: str,
    models: list[tuple[str, str]],
) -> dict:
    """Run all models against one URL concurrently. Returns a complete entry dict."""
    is_youtube = bool(_YOUTUBE_RE.match(url))
    entry: dict = {
        "url": url,
        "title": title,
        "kind": "youtube" if is_youtube else "article",
        "body": content,
        "summaries": [],
    }
    if not content:
        entry["fetch_error"] = True
        return entry
    entry["fetch_error"] = False
    model_tasks = [
        run_model(provider, model, url, title, content, is_youtube) for provider, model in models
    ]
    results = await asyncio.gather(*model_tasks, return_exceptions=True)
    for i, result in enumerate(results):
        provider, model = models[i]
        if isinstance(result, BaseException):
            entry["summaries"].append(
                {
                    "provider": provider,
                    "model": model,
                    "elapsed": None,
                    "summary": None,
                    "error": str(result),
                }
            )
        else:
            rprovider, rmodel, _, elapsed, summary = result
            entry["summaries"].append(
                {
                    "provider": rprovider,
                    "model": rmodel,
                    "elapsed": round(elapsed, 3),
                    "summary": summary,
                    "error": None,
                }
            )
    return entry


async def main() -> None:
    cfg = yaml.safe_load(_CONFIG_PATH.read_text())
    urls: list[str] = cfg["urls"]
    models: list[tuple[str, str]] = [(m["provider"], m["model"]) for m in cfg["models"]]

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = pathlib.Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"benchmark_{ts}.json"

    report: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "models": [{"provider": provider, "model": model} for provider, model in models],
        "results": [],
    }

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60),
        headers=_SESSION_HEADERS,
    ) as session:
        print("Fetching content from all URLs…")
        fetch_tasks = [fetch_content(url, session) for url in urls]
        contents = await asyncio.gather(*fetch_tasks)

    print(f"\nFetched {len(contents)} items. Running {len(models)} models on each.\n")

    all_entries = await asyncio.gather(
        *[process_url(url, title, content, models) for url, title, content in contents],
    )

    for entry in all_entries:
        is_youtube = entry["kind"] == "youtube"
        display = entry["title"] if entry["title"] else entry["url"]
        print(f"\n{'YouTube' if is_youtube else 'Article'}: {display}")
        print(f"URL: {entry['url']}")
        if entry.get("fetch_error"):
            print("  ERROR: could not fetch content — skipping")
        else:
            for s in entry["summaries"]:
                if s["error"]:
                    print(f"\n  [{s['provider']}/{s['model']}] ERROR: {s['error']}")
                else:
                    print(f"\n  [{s['provider']}/{s['model']}] ({s['elapsed']:.1f}s)")
                    if s["summary"]:
                        for line in s["summary"].splitlines():
                            print(f"  {line}")
                    else:
                        print("  (no output)")
        report["results"].append(entry)

    print("\nBenchmark complete.")
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
