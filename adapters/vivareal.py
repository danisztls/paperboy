import json
import logging

from playwright.async_api import Page

from pipeline import Item
from .base import SiteAdapter

log = logging.getLogger(__name__)

_BASE = "https://www.vivareal.com.br"

_TYPE_MAP = {
    "APARTMENT": "Apartamento",
    "HOME": "Casa",
    "CONDOMINIUM": "Condomínio",
    "FLAT": "Flat",
    "PENTHOUSE": "Cobertura",
    "STUDIO": "Studio",
    "KITNET": "Kitnet",
    "LAND": "Terreno",
    "FARM": "Fazenda",
    "COMMERCIAL": "Comercial",
    "OFFICE": "Escritório",
    "WAREHOUSE": "Galpão",
}

# Known paths to the listings array inside __NEXT_DATA__.props.pageProps
_NEXT_DATA_PATHS = [
    ["initialSearch", "result", "listings"],
    ["search", "result", "listings"],
    ["results", "listings"],
]


class VivaRealAdapter(SiteAdapter):
    @property
    def name(self) -> str:
        return "vivareal"

    async def scrape(self, url: str, cfg: dict, seen: set[str], page: Page) -> list[Item]:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass  # proceed even if network isn't fully idle

        items = self._from_next_data(await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent ?? null"
        ))
        if items is not None:
            log.info("[vivareal] %d listings via __NEXT_DATA__", len(items))
            return items

        log.warning("[vivareal] __NEXT_DATA__ extraction failed — falling back to DOM")
        items = await self._from_dom(page)
        log.info("[vivareal] %d listings via DOM", len(items))
        return items

    # --- __NEXT_DATA__ path ---

    def _from_next_data(self, raw: str | None) -> list[Item] | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        pp = data.get("props", {}).get("pageProps", {})
        for path in _NEXT_DATA_PATHS:
            node = pp
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, list):
                items = [self._parse_entry(e) for e in node]
                return [it for it in items if it is not None]

        log.debug("[vivareal] known __NEXT_DATA__ paths not found; pageProps keys: %s", list(pp))
        return None

    def _parse_entry(self, entry: dict) -> Item | None:
        # VivaReal wraps the listing under a "listing" key in search results
        listing = entry.get("listing") or entry
        try:
            lid = listing.get("id") or listing.get("externalId")
            if not lid:
                return None

            permalink = listing.get("permalink") or ""
            url = permalink if permalink.startswith("http") else f"{_BASE}{permalink}"
            if not url:
                return None

            def _first(lst):
                return lst[0] if isinstance(lst, list) and lst else None

            unit_types = listing.get("unitTypes") or []
            ltype = _TYPE_MAP.get(_first(unit_types) or "", "Imóvel")

            bedrooms = _first(listing.get("bedrooms"))
            bathrooms = _first(listing.get("bathrooms"))
            parking = _first(listing.get("parkingSpaces"))
            area = _first(listing.get("usableAreas")) or _first(listing.get("totalAreas"))

            addr = listing.get("address") or {}
            neighborhood = addr.get("neighborhood") or addr.get("zone") or ""
            city = addr.get("city") or ""
            location = ", ".join(p for p in [neighborhood, city] if p)

            pricing = _first(listing.get("pricingInfos"))
            price_raw = (pricing or {}).get("price") or (pricing or {}).get("monthlyCondoFee")
            try:
                price_str = f"R$ {int(float(price_raw)):,}".replace(",", ".") if price_raw else "Sob consulta"
            except (ValueError, TypeError):
                price_str = str(price_raw) if price_raw else "Sob consulta"

            title_parts = [ltype]
            if bedrooms:
                title_parts.append(f"{bedrooms}q")
            if location:
                title_parts.append(f"- {location}")

            body_parts = [price_str]
            if area:
                body_parts.append(f"{area}m²")
            if bedrooms:
                body_parts.append(f"{bedrooms} quartos")
            if bathrooms:
                body_parts.append(f"{bathrooms} ban.")
            if parking:
                body_parts.append(f"{parking} vaga")

            # medias can be on the listing or on the wrapper entry
            medias = listing.get("medias") or entry.get("medias") or []
            image = next(
                (m.get("url") or m.get("src") for m in medias if m.get("type") == "IMAGE"),
                None,
            ) or (medias[0].get("url") or medias[0].get("src") if medias else None)

            return Item(
                id=url,
                title=" ".join(title_parts)[:256],
                source="VivaReal",
                url=url,
                body=" · ".join(body_parts),
                image=image,
                meta={
                    "price": price_raw,
                    "area": area,
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "parking": parking,
                    "neighborhood": neighborhood,
                    "city": city,
                },
            )
        except Exception as exc:
            log.debug("[vivareal] Failed to parse entry: %s — %s", exc, str(entry)[:120])
            return None

    # --- DOM fallback ---

    async def _from_dom(self, page: Page) -> list[Item]:
        selector = '[data-type="property"], article.property-card, [data-id]'
        try:
            await page.wait_for_selector(selector, timeout=8000)
        except Exception:
            log.warning("[vivareal] No listing cards found via DOM selector")
            return []

        cards = await page.query_selector_all(selector)
        items: list[Item] = []
        for card in cards:
            try:
                link_el = await card.query_selector(
                    'a[href*="/imovel/"], a[href*="/venda/"], a[href*="/aluguel/"]'
                ) or await card.query_selector('a[href]')
                href = await link_el.get_attribute("href") if link_el else None
                if not href:
                    continue
                if not href.startswith("http"):
                    href = f"{_BASE}{href}"

                title_el = await card.query_selector('h2, [class*="title"], [data-cy*="title"]')
                title = (await title_el.inner_text()).strip() if title_el else href[:80]

                price_el = await card.query_selector('[class*="price"], [data-cy*="price"]')
                price_text = (await price_el.inner_text()).strip() if price_el else ""

                img_el = await card.query_selector("img[src], img[data-src]")
                image = None
                if img_el:
                    image = await img_el.get_attribute("src") or await img_el.get_attribute("data-src")

                items.append(Item(
                    id=href,
                    title=title[:256],
                    source="VivaReal",
                    url=href,
                    body=price_text,
                    image=image,
                ))
            except Exception as exc:
                log.debug("[vivareal] DOM card parse error: %s", exc)
                continue

        return items
