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
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from process.summarize import (
    _YOUTUBE_RE,
    _fetch_article,
    _fetch_youtube_data,
    summarize_entry,
    summarize_transcript,
)
from providers.llm import get_adapter

_CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"

_SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
}

console = Console()


async def fetch_content(url: str, session: aiohttp.ClientSession) -> tuple[str, str, str]:
    """Return (url, title, content) for a URL."""
    if _YOUTUBE_RE.match(url):
        result = await _fetch_youtube_data(url, session)
        if not result:
            return url, "", ""
        title, transcript = result
        return url, title, transcript
    result = await _fetch_article(url, session, with_title=True)
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


def _print_entry(entry: dict) -> None:
    kind = entry["kind"].capitalize()
    title_or_url = entry["title"] if entry["title"] else entry["url"]
    border = "cyan" if entry["kind"] == "youtube" else "green"

    if entry.get("fetch_error"):
        console.print(
            Panel(
                f"[dim]{escape(entry['url'])}[/dim]\n\n[red]ERROR: could not fetch content[/red]",
                title=f"[bold]{kind}:[/bold] {escape(title_or_url[:80])}",
                border_style="red",
            )
        )
        return

    parts: list[str] = []
    for s in entry["summaries"]:
        label = f"[bold]{escape(s['provider'])}/{escape(s['model'])}[/bold]"
        if s["error"]:
            parts.append(f"{label}\n[red]ERROR: {escape(s['error'])}[/red]")
        else:
            elapsed_str = f"[dim]{s['elapsed']:.1f}s[/dim]"
            body = (s["summary"] or "(no output)").strip()
            parts.append(f"{label} {elapsed_str}\n{escape(body)}")

    console.print(
        Panel(
            "\n\n".join(parts),
            title=f"[bold]{kind}:[/bold] {escape(title_or_url[:80])}",
            subtitle=f"[dim]{escape(entry['url'][:100])}[/dim]",
            border_style=border,
        )
    )


def _print_summary_table(all_entries: list[dict], models: list[tuple[str, str]]) -> None:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    table.add_column("URL", max_width=40, no_wrap=True)

    model_labels = [f"{p}/{m}" for p, m in models]
    for label in model_labels:
        table.add_column(label, justify="right", min_width=10)

    for entry in all_entries:
        title_or_url = (entry["title"] or entry["url"])[:40]
        if entry.get("fetch_error"):
            row = [Text(title_or_url, style="dim")] + [Text("fetch err", style="red")] * len(models)
            table.add_row(*row)
            continue

        by_model = {f"{s['provider']}/{s['model']}": s for s in entry["summaries"]}
        cells: list[Text] = [Text(title_or_url)]
        for label in model_labels:
            s = by_model.get(label)
            if s is None:
                cells.append(Text("—", style="dim"))
            elif s["error"]:
                cells.append(Text("error", style="red"))
            else:
                cells.append(Text(f"{s['elapsed']:.1f}s", style="green"))
        table.add_row(*cells)

    console.print(Rule("[bold]Elapsed times[/bold]"))
    console.print(table)


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
        "models": [{"provider": p, "model": m} for p, m in models],
        "results": [],
    }

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60),
        headers=_SESSION_HEADERS,
    ) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            fetch_task = progress.add_task("Fetching content…", total=len(urls))

            async def _fetch_one(url: str) -> tuple[str, str, str]:
                result = await fetch_content(url, session)
                progress.advance(fetch_task)
                return result

            contents = await asyncio.gather(*[_fetch_one(url) for url in urls])

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        run_task = progress.add_task(
            f"Running {len(models)} model(s) on each URL…", total=len(contents)
        )

        async def _process_one(url: str, title: str, content: str) -> dict:
            entry = await process_url(url, title, content, models)
            progress.advance(run_task)
            return entry

        all_entries = await asyncio.gather(
            *[_process_one(url, title, content) for url, title, content in contents]
        )

    console.print()
    for entry in all_entries:
        _print_entry(entry)
        report["results"].append(entry)

    if len(all_entries) > 1:
        _print_summary_table(list(all_entries), models)

    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    console.print(f"\n[dim]Saved to {out_path}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
