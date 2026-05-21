import json
import logging
import re

from playwright.async_api import Page

from pipeline import Item

from .base import SiteAdapter, register_adapter

log = logging.getLogger(__name__)

# DO NOT ASSUME __NEXT_DATA__ EXISTS. VivaReal's pages run on the Next.js App
# Router and never emit a <script id="__NEXT_DATA__"> tag (it's a Pages-Router
# artifact). If you reach for __NEXT_DATA__ here, you'll get None back and
# silently lose data. Use the rendered DOM / JSON-LD `ItemList` instead. This
# trap has been stepped in twice; don't make it three.
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


def _dedup_images(urls, limit: int = 4) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls or []:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except ValueError, TypeError:
        return None


def _passes_area_per_room(item: Item, min_ratio: float) -> bool:
    """Drop listings whose area / bedrooms is below `min_ratio` (m²/quarto).
    Listings missing either field pass — can't evaluate the ratio."""
    area = _as_int(item.meta.get("area"))
    bedrooms = _as_int(item.meta.get("bedrooms"))
    if not area or not bedrooms:
        return True
    return (area / bedrooms) >= min_ratio


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
            min_area_per_room = cfg.get("min_area_per_room")
            if min_area_per_room is not None:
                before = len(items)
                items = [it for it in items if _passes_area_per_room(it, min_area_per_room)]
                log.info(
                    "[vivareal] %d listings via JSON-LD (%d → %d after min_area_per_room=%s m²/quarto)",
                    before,
                    before,
                    len(items),
                    min_area_per_room,
                )
            else:
                log.info("[vivareal] %d listings via JSON-LD", len(items))
            return items

        log.warning("[vivareal] No JSON-LD ItemList found")
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

            raw_images = item.get("image") or []
            if isinstance(raw_images, str):
                raw_images = [raw_images]
            elif not isinstance(raw_images, list):
                raw_images = []
            images = _dedup_images(raw_images)
            image = images[0] if images else None

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
                images=images,
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
