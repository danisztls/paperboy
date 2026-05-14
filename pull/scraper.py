"""Web scraping source: browser-based extraction via site adapters."""

import logging

from playwright.async_api import async_playwright

from pipeline import PullResult, Source
from pull.adapters import vivareal  # noqa: F401 — registers via @register_adapter
from pull.adapters.base import available_adapters, get_adapter

log = logging.getLogger(__name__)


def _get_scraper_cfg(task_cfg: dict) -> dict:
    return next((item["scraper"] for item in task_cfg.get("pull", []) if "scraper" in item), {})


class ScraperSource(Source):
    async def pull(self, cfg: dict, seen: set[str], session) -> PullResult | None:
        scraper_cfg = _get_scraper_cfg(cfg)
        adapter_name = scraper_cfg.get("adapter", "")
        url = scraper_cfg.get("url", "")
        max_items = scraper_cfg.get("max_items")

        adapter_cls = get_adapter(adapter_name)
        if not adapter_cls:
            log.error(
                "[scraper] Unknown adapter %r. Available: %s", adapter_name, available_adapters()
            )
            return None
        if not url:
            log.error("[scraper] No url configured for adapter %r", adapter_name)
            return None

        log.info("[scraper] %s → %s", adapter_name, url)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="pt-BR",
                        viewport={"width": 1280, "height": 900},
                    )
                    page = await context.new_page()
                    all_items = await adapter_cls().scrape(url, scraper_cfg, seen, page)
                finally:
                    await browser.close()
        except Exception as exc:
            log.error("[scraper] %s failed: %s", adapter_name, exc)
            return None

        current = [{"url": it.id, "title": it.title} for it in all_items]
        new_items = [it for it in all_items if it.id not in seen]
        if max_items is not None:
            new_items = new_items[:max_items]

        log.info(
            "[scraper] %d total, %d new (capped: %s)", len(all_items), len(new_items), max_items
        )
        return PullResult(new_items=new_items, current_items=current)
