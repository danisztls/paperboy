import logging
import re

from playwright.async_api import Page

from pipeline import Item

from .base import SiteAdapter, register_adapter

log = logging.getLogger(__name__)

# Server-rendered PHP listing page; no JSON-LD, no JS gating. Each listing card is
# `.pgl-property`; specs (price/category/title/beds/parking) all live in the card
# and there's no need to visit the detail page. `_EXTRACT_JS` runs in-page and
# returns a list of dicts which the adapter maps to Items.
_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('.pgl-property')).map(card => {
    const linkEl = card.querySelector('.property-thumb-info-image a[href]');
    const imgEl = card.querySelector('.property-thumb-info-image img');
    const titleEl = card.querySelector('address a');
    const contentEl = card.querySelector('.property-thumb-info-content');
    const category = contentEl
        ? [...contentEl.childNodes]
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim())
            .filter(Boolean)
            .join(' ')
        : '';
    const amenities = card.querySelectorAll('.amenities .pull-right li');
    return {
        url: linkEl ? linkEl.href : null,
        image: imgEl ? imgEl.src : null,
        title: titleEl ? titleEl.textContent.trim() : '',
        price: card.querySelector('.label.price')?.textContent.trim() || null,
        label: card.querySelector('.label.forrent')?.textContent.trim() || null,
        category: category || null,
        bedrooms: amenities[0]?.textContent.trim() || null,
        parking: amenities[1]?.textContent.trim() || null,
    };
})
"""

# Detail page exposes the full gallery at `_848.jpeg`. Card thumbnails are `_360.jpeg`.
_GALLERY_JS = """
() => {
    const urls = [...document.querySelectorAll('img[src*="_848.jpeg"]')]
        .map(i => new URL(i.getAttribute('src'), location.href).toString());
    return [...new Set(urls)];
}
"""
_GALLERY_CAP = 4


def _as_int(value) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _format_body(price, label, bedrooms, parking, category) -> str:
    line1: list[str] = []
    if price:
        line1.append(price)
    bd = _as_int(bedrooms)
    if bd is not None:
        line1.append(f"{bd} {'quarto' if bd == 1 else 'quartos'}")
    pk = _as_int(parking)
    if pk is not None:
        line1.append(f"{pk} {'vaga' if pk == 1 else 'vagas'}")
    line2: list[str] = []
    if category:
        line2.append(category)
    if label:
        line2.append(label)
    lines = []
    if line1:
        lines.append(" · ".join(line1))
    if line2:
        lines.append(" · ".join(line2))
    return "\n".join(lines)


@register_adapter("imoveis_romildo")
class ImoveisRomildoAdapter(SiteAdapter):
    @property
    def name(self) -> str:
        return "imoveis_romildo"

    async def scrape(self, url: str, cfg: dict, seen: set[str], page: Page) -> list[Item]:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            cards = await page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            log.error("[imoveis_romildo] DOM extraction failed: %s", exc)
            return []

        items: list[Item] = []
        for c in cards:
            detail_url = c.get("url")
            title = c.get("title") or ""
            if not detail_url or not title:
                continue
            bedrooms = _as_int(c.get("bedrooms"))
            parking = _as_int(c.get("parking"))
            thumb = c.get("image")
            images = [thumb] if thumb else []
            if detail_url not in seen:
                gallery = await _fetch_gallery(page, detail_url)
                if gallery:
                    images = gallery
            items.append(
                Item(
                    id=detail_url,
                    title=title[:256],
                    source="Romildo Binda",
                    url=detail_url,
                    body=_format_body(
                        c.get("price"),
                        c.get("label"),
                        bedrooms,
                        parking,
                        c.get("category"),
                    ),
                    image=images[0] if images else None,
                    images=images,
                    meta={
                        "price": c.get("price"),
                        "label": c.get("label"),
                        "category": c.get("category"),
                        "bedrooms": bedrooms,
                        "parking": parking,
                    },
                )
            )
        log.info("[imoveis_romildo] %d listings", len(items))
        return items


async def _fetch_gallery(page: Page, detail_url: str) -> list[str]:
    """Visit a listing detail page and return up to `_GALLERY_CAP` large image URLs."""
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        urls = await page.evaluate(_GALLERY_JS)
    except Exception as exc:
        log.warning("[imoveis_romildo] gallery fetch failed for %s: %s", detail_url, exc)
        return []
    if not isinstance(urls, list):
        return []
    out: list[str] = []
    seen_local: set[str] = set()
    for u in urls:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s or s in seen_local:
            continue
        seen_local.add(s)
        out.append(s)
        if len(out) >= _GALLERY_CAP:
            break
    return out
