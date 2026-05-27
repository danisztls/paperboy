import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline import Item
from process._vasco import fetch_raw_html

from .base import SiteAdapter, register_adapter

log = logging.getLogger(__name__)

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


def _extract_cards(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for card in soup.select(".pgl-property"):
        link_el = card.select_one(".property-thumb-info-image a[href]")
        img_el = card.select_one(".property-thumb-info-image img")
        title_el = card.select_one("address a")
        content_el = card.select_one(".property-thumb-info-content")
        category = ""
        if content_el:
            category = " ".join(node.strip() for node in content_el.strings if node.strip())
        amenity_els = card.select(".amenities .pull-right li")
        cards.append(
            {
                "url": urljoin(base_url, link_el["href"])
                if link_el and link_el.get("href")
                else None,
                "image": img_el.get("src") if img_el else None,
                "title": title_el.get_text(strip=True) if title_el else "",
                "price": (card.select_one(".label.price") or {}).get_text(strip=True)
                if card.select_one(".label.price")
                else None,
                "label": (card.select_one(".label.forrent") or {}).get_text(strip=True)
                if card.select_one(".label.forrent")
                else None,
                "category": category or None,
                "bedrooms": amenity_els[0].get_text(strip=True) if len(amenity_els) > 0 else None,
                "parking": amenity_els[1].get_text(strip=True) if len(amenity_els) > 1 else None,
            }
        )
    return cards


def _extract_gallery(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for img in soup.select('img[src*="_848.jpeg"]'):
        src = img.get("src")
        if not src:
            continue
        url = urljoin(base_url, src)
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= _GALLERY_CAP:
            break
    return out


@register_adapter("imoveis_romildo")
class ImoveisRomildoAdapter(SiteAdapter):
    @property
    def name(self) -> str:
        return "imoveis_romildo"

    async def scrape(self, url: str, cfg: dict, seen: set[str], html: str) -> list[Item]:
        cards = _extract_cards(html, url)

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
                gallery = await _fetch_gallery(detail_url)
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


async def _fetch_gallery(detail_url: str) -> list[str]:
    html = await fetch_raw_html(detail_url)
    if not html:
        log.warning("[imoveis_romildo] gallery fetch failed for %s", detail_url)
        return []
    return _extract_gallery(html, detail_url)
