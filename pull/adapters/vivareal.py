import json
import logging
import re

from playwright.async_api import Page

from pipeline import Item

from .base import SiteAdapter, register_adapter

log = logging.getLogger(__name__)

_BASE = "https://www.vivareal.com.br"

_JSONLD_TYPE_MAP = {
    "Apartment": "Apartamento",
    "House": "Casa",
    "SingleFamilyResidence": "Casa",
    "Condominium": "Condomínio",
    "Flat": "Flat",
    "Penthouse": "Cobertura",
    "Place": "Imóvel",
    "Accommodation": "Imóvel",
    "Residence": "Imóvel",
    "LandProperty": "Terreno",
}

# Legacy: kept for __NEXT_DATA__ fallback
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

_NEXT_DATA_PATHS = [
    ["initialSearch", "result", "listings"],
    ["search", "result", "listings"],
    ["results", "listings"],
]


@register_adapter("vivareal")
class VivaRealAdapter(SiteAdapter):
    @property
    def name(self) -> str:
        return "vivareal"

    async def scrape(self, url: str, cfg: dict, seen: set[str], page: Page) -> list[Item]:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        # Primary: JSON-LD ItemList (current site structure)
        jsonld_blocks = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))"
            ".map(s => s.textContent)"
        )
        items = self._from_jsonld(jsonld_blocks)
        if items is not None:
            log.info("[vivareal] %d listings via JSON-LD", len(items))
            return items

        # Fallback: __NEXT_DATA__ (legacy Next.js Pages Router)
        raw = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent ?? null"
        )
        items = self._from_next_data(raw)
        if items is not None:
            log.info("[vivareal] %d listings via __NEXT_DATA__", len(items))
            return items

        log.warning("[vivareal] No listing data found (tried JSON-LD and __NEXT_DATA__)")
        return []

    # --- JSON-LD path (primary) ---

    def _from_jsonld(self, blocks: list[str]) -> list[Item] | None:
        for raw in blocks:
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if d.get("@type") != "ItemList":
                continue
            elements = d.get("itemListElement") or []
            if not elements:
                return None
            items = [self._parse_jsonld_entry(e.get("item", e)) for e in elements]
            return [it for it in items if it is not None]
        return None

    def _parse_jsonld_entry(self, item: dict) -> Item | None:
        try:
            url = item.get("url") or ""
            if not url:
                return None

            ltype = _JSONLD_TYPE_MAP.get(item.get("@type", ""), "Imóvel")

            bedrooms = item.get("numberOfBedrooms")
            bathrooms = item.get("numberOfBathroomsTotal")
            area = (item.get("floorSize") or {}).get("value")

            name = item.get("name") or ""
            # Name format: "Tipo ... em Bairro, Cidade" — extract neighborhood
            nbh_match = re.search(r"\bem ([^,]+),", name)
            neighborhood = nbh_match.group(1).strip() if nbh_match else ""

            addr = item.get("address") or {}
            city = addr.get("addressLocality") or ""
            location = ", ".join(p for p in [neighborhood, city] if p)

            # Parking not in JSON-LD schema; parse from name
            parking_match = re.search(r"(\d+)\s*vaga", name, re.IGNORECASE)
            parking = int(parking_match.group(1)) if parking_match else None

            offers = item.get("offers") or {}
            price_raw = offers.get("price")
            try:
                price_str = (
                    f"R$ {int(float(price_raw)):,}".replace(",", ".")
                    if price_raw
                    else "Sob consulta"
                )
            except ValueError, TypeError:
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

            images = item.get("image") or []
            image = (
                images[0]
                if isinstance(images, list) and images
                else (images if isinstance(images, str) else None)
            )

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
            log.debug("[vivareal] Failed to parse JSON-LD entry: %s — %s", exc, str(item)[:120])
            return None

    # --- __NEXT_DATA__ path (legacy fallback) ---

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
                items = [self._parse_next_data_entry(e) for e in node]
                return [it for it in items if it is not None]

        log.warning("[vivareal] __NEXT_DATA__ paths not found; pageProps keys: %s", list(pp))
        return None

    def _parse_next_data_entry(self, entry: dict) -> Item | None:
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
                price_str = (
                    f"R$ {int(float(price_raw)):,}".replace(",", ".")
                    if price_raw
                    else "Sob consulta"
                )
            except ValueError, TypeError:
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
            log.debug(
                "[vivareal] Failed to parse __NEXT_DATA__ entry: %s — %s", exc, str(entry)[:120]
            )
            return None
