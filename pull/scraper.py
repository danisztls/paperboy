"""Web scraping source: browser-based extraction via site adapters.

Uses Camoufox (hardened Firefox) as the engine so adapters get past anti-bot
protections like Cloudflare without per-adapter stealth plumbing. Camoufox
manages the fingerprint itself — don't set a custom User-Agent. Requires the
binary downloaded once via `uv run camoufox fetch`.

Scraper tasks may have multiple `scraper:` items in their `pull:` list — one
per adapter, each with its own URL. `pull_scrapers` runs them all under one
Camoufox browser (serial pages), with per-adapter exception isolation so a
broken selector on one site doesn't kill the others.
"""

import logging

from camoufox.async_api import AsyncCamoufox

from pipeline import PullResult
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
    """Run all configured scrapers under one Camoufox browser.

    Returns `{adapter_name: PullResult | None}`. A `None` value means that
    adapter failed (unknown adapter, missing url, navigation/extraction error)
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

    try:
        async with AsyncCamoufox(headless=True, locale="pt-BR", os="linux") as browser:
            for adapter_name, sc in valid:
                url = sc["url"]
                max_items = sc.get("max_items")
                seen = seen_per_adapter.get(adapter_name, set())
                log.info("[scraper] %s → %s", adapter_name, url)
                page = await browser.new_page()
                try:
                    all_items = await get_adapter(adapter_name)().scrape(url, sc, seen, page)
                except Exception as exc:
                    log.error("[scraper] %s failed: %s", adapter_name, exc)
                    results[adapter_name] = None
                    continue
                finally:
                    await page.close()

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
    except Exception as exc:
        log.error("[scraper] Browser launch failed: %s", exc)
        for adapter_name, _ in valid:
            results.setdefault(adapter_name, None)

    return results
