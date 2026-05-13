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
    _fetch_article_content,
    _fetch_youtube_data,
    summarize_entry,
    summarize_transcript,
)

_CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"

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
    url: str,
    title: str,
    content: str,
    is_youtube: bool,
) -> tuple[str, str, str, float, str | None]:
    """Return (provider, model, url, elapsed_seconds, summary)."""
    adapter = get_adapter(provider)
    start = time.monotonic()
    if is_youtube:
        summary = await summarize_transcript(title, content, adapter, model=model)
    else:
        summary = await summarize_entry(title or url, content, adapter, model=model)
    elapsed = time.monotonic() - start
    return provider, model, url, elapsed, summary


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

    for url, title, content in contents:
        is_youtube = bool(_YOUTUBE_RE.match(url))
        kind = "youtube" if is_youtube else "article"
        display = title if title else url
        print(f"\n{'YouTube' if is_youtube else 'Article'}: {display}")
        print(f"URL: {url}")

        entry: dict = {
            "url": url,
            "title": title,
            "kind": kind,
            "summaries": [],
        }

        if not content:
            print("  ERROR: could not fetch content — skipping")
            entry["fetch_error"] = True
            report["results"].append(entry)
            continue

        entry["fetch_error"] = False

        model_tasks = [
            run_model(provider, model, url, title, content, is_youtube)
            for provider, model in models
        ]
        results = await asyncio.gather(*model_tasks, return_exceptions=True)

        for i, result in enumerate(results):
            provider, model = models[i]
            if isinstance(result, BaseException):
                print(f"\n  [{provider}/{model}] ERROR: {result}")
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
                print(f"\n  [{rprovider}/{rmodel}] ({elapsed:.1f}s)")
                if summary:
                    for line in summary.splitlines():
                        print(f"  {line}")
                else:
                    print("  (no output)")
                entry["summaries"].append(
                    {
                        "provider": rprovider,
                        "model": rmodel,
                        "elapsed": round(elapsed, 3),
                        "summary": summary,
                        "error": None,
                    }
                )

        report["results"].append(entry)

    print("\nBenchmark complete.")
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
