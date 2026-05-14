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

# DO NOT ASSUME __NEXT_DATA__ EXISTS. VivaReal's listing detail pages run on the
# Next.js App Router and never emit a <script id="__NEXT_DATA__"> tag (the script
# is a Pages-Router artifact). Empirically the search-results page still ships it
# today, so `_from_next_data` is wired up as a fallback there only — but treat it
# as a bonus, not a contract. If you reach for __NEXT_DATA__ on a detail page you
# will get None back and silently lose data. Use the rendered DOM (innerText /
# JSON-LD `Product`) instead. This trap has been stepped in twice; don't make it
# three.
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

# Curated whitelist for `offers.amenityFeature`. Drops table-stakes amenities
# (Kitchen, Laundry, Garage, Service Area) and keeps the differentiators.
# Keys are the English values VivaReal emits; values are the rendered labels.
_AMENITY_LABELS = {
    "Pool": "Piscina",
    "Gated Community": "Condomínio Fechado",
    "Elevator": "Elevador",
    "Balcony": "Varanda",
    "Furnished": "Mobiliado",
    "Party Hall": "Salão de Festas",
    "Barbecue Grill": "Churrasqueira",
    "Playground": "Playground",
    "Pets Allowed": "Aceita Pets",
}


def _fmt_brl(value) -> str | None:
    try:
        return f"R$ {int(float(value)):,}".replace(",", ".")
    except ValueError, TypeError:
        return None


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except ValueError, TypeError:
        return None


def _format_price(rent, condo, iptu) -> str:
    rent_s = _fmt_brl(rent)
    if rent_s is None:
        return "Sob consulta"
    rent_i = _as_int(rent) or 0

    extras_s: list[str] = []
    total = rent_i
    for v in (condo, iptu):
        v_i = _as_int(v)
        if v_i is None or v_i <= 0:
            continue
        extras_s.append(_fmt_brl(v_i))
        total += v_i

    if not extras_s:
        return rent_s
    chain = " + ".join([rent_s, *extras_s])
    return f"{chain} = {_fmt_brl(total)}"


def _extract_condo_fee(pv) -> int | None:
    """Pull the Condominium Fee value out of `offers.propertyValue` (dict or list)."""
    if isinstance(pv, dict):
        if pv.get("name") == "Condominium Fee":
            return _as_int(pv.get("value"))
        return None
    if isinstance(pv, list):
        for entry in pv:
            if isinstance(entry, dict) and entry.get("name") == "Condominium Fee":
                return _as_int(entry.get("value"))
    return None


def _extract_amenities(features) -> list[str]:
    """Map `amenityFeature` entries through the whitelist; drop unknowns."""
    if not isinstance(features, list):
        return []
    out: list[str] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        key = f.get("value") or f.get("name")
        label = _AMENITY_LABELS.get(key)
        if label and label not in out:
            out.append(label)
    return out


def _format_body(
    rent,
    condo,
    iptu,
    area,
    bedrooms,
    bathrooms,
    parking,
    *,
    street: str | None = None,
    amenities: list[str] | None = None,
) -> str:
    parts: list[str] = [_format_price(rent, condo, iptu)]
    if area:
        parts.append(f"{area}m²")
    if bedrooms:
        parts.append(f"{bedrooms} quartos")
    if bathrooms:
        parts.append(f"{bathrooms} ban.")
    if parking:
        parts.append(f"{parking} vaga")
    lines = [" · ".join(parts)]
    if amenities:
        lines.append(" · ".join(amenities))
    if street:
        lines.append(street)
    return "\n".join(lines)


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

        # Primary: JSON-LD ItemList — carries everything we need per listing
        # (rent via offers.price, condo fee via offers.propertyValue, first gallery
        # image, address.streetAddress, curated amenityFeature values). Detail pages
        # are gated by Cloudflare; we don't visit them.
        jsonld_blocks = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))"
            ".map(s => s.textContent)"
        )
        items = self._from_jsonld(jsonld_blocks)
        if items is not None:
            log.info("[vivareal] %d listings via JSON-LD", len(items))
            return items

        # Fallback: __NEXT_DATA__ (legacy Next.js Pages Router on the search page)
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
            street = (addr.get("streetAddress") or "").strip() or None
            location = ", ".join(p for p in [neighborhood, city] if p)

            # Parking not in JSON-LD schema; parse from name
            parking_match = re.search(r"(\d+)\s*vaga", name, re.IGNORECASE)
            parking = int(parking_match.group(1)) if parking_match else None

            offers = item.get("offers") or {}
            price_raw = offers.get("price")
            condo_raw = _extract_condo_fee(offers.get("propertyValue"))

            amenities = _extract_amenities(item.get("amenityFeature"))

            title_parts = [ltype]
            if bedrooms:
                title_parts.append(f"{bedrooms}q")
            if location:
                title_parts.append(f"- {location}")

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
                body=_format_body(
                    price_raw,
                    condo_raw,
                    None,
                    area,
                    bedrooms,
                    bathrooms,
                    parking,
                    street=street,
                    amenities=amenities,
                ),
                image=image,
                meta={
                    "price": price_raw,
                    "condo_fee": condo_raw,
                    "iptu": None,
                    "area": area,
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "parking": parking,
                    "neighborhood": neighborhood,
                    "city": city,
                    "street": street,
                    "amenities": amenities,
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

            pricing = _first(listing.get("pricingInfos")) or {}
            price_raw = pricing.get("price")
            condo_raw = pricing.get("monthlyCondoFee")
            iptu_yearly = _as_int(pricing.get("yearlyIptu"))
            iptu_raw = pricing.get("monthlyIptu") or (iptu_yearly // 12 if iptu_yearly else None)
            street = (addr.get("street") or "").strip() or None

            title_parts = [ltype]
            if bedrooms:
                title_parts.append(f"{bedrooms}q")
            if location:
                title_parts.append(f"- {location}")

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
                body=_format_body(
                    price_raw,
                    condo_raw,
                    iptu_raw,
                    area,
                    bedrooms,
                    bathrooms,
                    parking,
                    street=street,
                ),
                image=image,
                meta={
                    "price": price_raw,
                    "condo_fee": condo_raw,
                    "iptu": iptu_raw,
                    "area": area,
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "parking": parking,
                    "neighborhood": neighborhood,
                    "city": city,
                    "street": street,
                },
            )
        except Exception as exc:
            log.debug(
                "[vivareal] Failed to parse __NEXT_DATA__ entry: %s — %s", exc, str(entry)[:120]
            )
            return None
