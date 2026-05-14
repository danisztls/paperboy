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

_DESC_TRUNCATE = 500


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


def _truncate(text: str, n: int = _DESC_TRUNCATE) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def _parse_brl(text: str) -> int | None:
    """Parse 'R$ 1.234,56' (Brazilian format) into integer BRL."""
    if not text:
        return None
    cleaned = text.strip().replace("R$", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _format_body(
    rent, condo, iptu, area, bedrooms, bathrooms, parking, description: str | None
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
    line = " · ".join(parts)
    desc = _truncate(description or "")
    return f"{line}\n\n{desc}" if desc else line


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
            await self._enrich_new(items, cfg, seen, page)
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

    # --- Per-listing enrichment ---

    async def _enrich_new(self, items: list[Item], cfg: dict, seen: set[str], page: Page) -> None:
        max_items = cfg.get("max_items")
        new_items = [it for it in items if it.id not in seen]
        if max_items is not None:
            new_items = new_items[:max_items]
        if not new_items:
            return
        log.info("[vivareal] enriching %d new listing(s)", len(new_items))
        for it in new_items:
            await self._enrich_listing(it, page)

    async def _enrich_listing(self, item: Item, page: Page) -> None:
        try:
            await page.goto(item.url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            jsonld_blocks = await page.evaluate(
                "() => Array.from("
                "document.querySelectorAll('script[type=\"application/ld+json\"]'))"
                ".map(s => s.textContent)"
            )
            description, gallery_image = self._extract_listing_jsonld(jsonld_blocks)
            if gallery_image:
                item.image = gallery_image
            if description:
                item.meta["description"] = description

            # Condo + IPTU live in the visible price-breakdown block. Labels and
            # values sit in sibling DOM nodes (`.value-item__tooltip-wrapper`),
            # so they're adjacent in innerText but not in raw HTML.
            text = await page.evaluate("() => document.body?.innerText ?? ''")
            condo = iptu = None
            m = re.search(r"Condom[íi]nio\s+R\$\s*([\d.,]+)", text, re.IGNORECASE)
            if m:
                condo = _parse_brl(m.group(1))
            m = re.search(r"IPTU\s+R\$\s*([\d.,]+)", text, re.IGNORECASE)
            if m:
                iptu = _parse_brl(m.group(1))

            if condo is not None:
                item.meta["condo_fee"] = condo
            if iptu is not None:
                item.meta["iptu"] = iptu

            item.body = _format_body(
                item.meta.get("price"),
                item.meta.get("condo_fee"),
                item.meta.get("iptu"),
                item.meta.get("area"),
                item.meta.get("bedrooms"),
                item.meta.get("bathrooms"),
                item.meta.get("parking"),
                item.meta.get("description"),
            )
            log.info(
                "[vivareal] enriched %s — desc=%s img=%s condo=%s iptu=%s",
                item.url,
                "Y" if description else "N",
                "Y" if gallery_image else "N",
                condo,
                iptu,
            )
        except Exception as exc:
            log.warning("[vivareal] enrich failed for %s: %r", item.url, exc)

    def _extract_listing_jsonld(self, blocks: list[str]) -> tuple[str | None, str | None]:
        """From listing-page JSON-LD, return (description, first_image)."""
        for raw in blocks:
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            candidates = d.get("@graph") if isinstance(d, dict) and "@graph" in d else [d]
            for c in candidates or []:
                if not isinstance(c, dict):
                    continue
                # Listing detail pages use @type "Product"; older variants use Apartment/House/etc.
                if c.get("@type") != "Product" and c.get("@type") not in _JSONLD_TYPE_MAP:
                    continue
                desc = c.get("description")
                first = None
                imgs = c.get("image")
                if isinstance(imgs, str):
                    first = imgs
                elif isinstance(imgs, list) and imgs:
                    head = imgs[0]
                    if isinstance(head, str):
                        first = head
                    elif isinstance(head, dict):
                        first = head.get("url") or head.get("contentUrl")
                if desc or first:
                    return desc, first
        return None, None

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
                body=_format_body(price_raw, None, None, area, bedrooms, bathrooms, parking, None),
                image=image,
                meta={
                    "price": price_raw,
                    "condo_fee": None,
                    "iptu": None,
                    "description": None,
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

            pricing = _first(listing.get("pricingInfos")) or {}
            price_raw = pricing.get("price")
            condo_raw = pricing.get("monthlyCondoFee")
            iptu_yearly = _as_int(pricing.get("yearlyIptu"))
            iptu_raw = pricing.get("monthlyIptu") or (iptu_yearly // 12 if iptu_yearly else None)
            description = listing.get("description")

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
                    description,
                ),
                image=image,
                meta={
                    "price": price_raw,
                    "condo_fee": condo_raw,
                    "iptu": iptu_raw,
                    "description": description,
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
