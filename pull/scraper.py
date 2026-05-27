"""Web scraping source: fetches pages via vasco and parses with site adapters.

Vasco handles the transport layer (HTTP with auto-escalation to browser on
bot-blocked sites, SQLite caching, domain strategy). Adapters parse the
returned raw HTML with BeautifulSoup to extract structured listing data.

JS-rendered pages (e.g. Elementor) need ``mode: browser`` in the scraper
config to force vasco to use a headless browser instead of plain HTTP.
"""

import asyncio
import logging

from pipeline import PullResult
from process._vasco import fetch_raw_html
from pull.scrapers import (  # noqa: F401 — registers via @register_adapter
    portal_b,
    portal_a,
    vivareal,
)
from pull.scrapers.base import available_adapters, get_adapter

log = logging.getLogger(__name__)


def _get_scraper_cfgs(task_cfg: dict) -> list[dict]:
    return [item["scraper"] for item in task_cfg.get("pull", []) if "scraper" in item]


async def pull_scrapers(
    scraper_cfgs: list[dict],
    seen_per_adapter: dict[str, set[str]],
) -> dict[str, PullResult | None]:
    """Run all configured scrapers via vasco-fetched HTML.

    Returns `{adapter_name: PullResult | None}`. A `None` value means that
    adapter failed (unknown adapter, missing url, fetch/extraction error)
    and the caller must preserve its prior state. Adapters without a `seen`
    entry get an empty set.
    """
    results: dict[str, PullResult | None] = {}

    valid: list[tuple[str, dict]] = []
    for sc in scraper_cfgs:
        adapter_name = sc.get("adapter", "")
        url = sc.get("url", "")
        if not get_adapter(adapter_name):
            log.error(
                "[scraper] Unknown adapter %r. Available: %s", adapter_name, available_adapters()
            )
            results[adapter_name] = None
            continue
        if not url:
            log.error("[scraper] No url configured for adapter %r", adapter_name)
            results[adapter_name] = None
            continue
        valid.append((adapter_name, sc))

    if not valid:
        return results

    async def _fetch(sc: dict) -> tuple[str, str | None]:
        url = sc["url"]
        mode = sc.get("mode", "auto")
        return url, await fetch_raw_html(url, mode=mode)

    fetched = await asyncio.gather(*[_fetch(sc) for _, sc in valid])
    html_map = {url: html for url, html in fetched}

    for adapter_name, sc in valid:
        url = sc["url"]
        max_items = sc.get("max_items")
        seen = seen_per_adapter.get(adapter_name, set())
        log.info("[scraper] %s → %s", adapter_name, url)

        html = html_map.get(url)
        if not html:
            log.error("[scraper] %s: failed to fetch %s", adapter_name, url)
            results[adapter_name] = None
            continue

        try:
            all_items = await get_adapter(adapter_name)().scrape(url, sc, seen, html)
        except Exception as exc:
            log.error("[scraper] %s failed: %s", adapter_name, exc)
            results[adapter_name] = None
            continue

        current = [{"url": it.id, "title": it.title} for it in all_items]
        new_items = [it for it in all_items if it.id not in seen]
        if max_items is not None:
            new_items = new_items[:max_items]
        log.info(
            "[scraper] %s: %d total, %d new (capped: %s)",
            adapter_name,
            len(all_items),
            len(new_items),
            max_items,
        )
        results[adapter_name] = PullResult(new_items=new_items, current_items=current)

    return results
