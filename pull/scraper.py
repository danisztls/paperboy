"""Web scraping source: structured real-estate listings via vasco.

vasco's realestate adapter (``mode_used="realestate"``) parses each portal
(vivareal / binda / barreto) into normalized listing dicts; this module maps
them to pipeline ``Item``s and applies claudinho-side policy
(``min_area_per_room`` filter, ``max_items`` cap, ``seen`` dedup).

vasco picks the parser by domain, so a scraper config just needs a ``url``.
The ``adapter`` field is now only a stable per-source id used to key state.
"""

import asyncio
import logging

from pipeline import Item, PullResult
from process._vasco import fetch_listings

log = logging.getLogger(__name__)

# Structured fields carried through to Item.meta (for filtering + downstream).
_META_FIELDS = (
    "price",
    "condo_fee",
    "iptu",
    "area",
    "bedrooms",
    "bathrooms",
    "parking",
    "neighborhood",
    "city",
    "street",
    "type",
    "amenities",
)


def _get_scraper_cfgs(task_cfg: dict) -> list[dict]:
    return [item["scraper"] for item in task_cfg.get("pull", []) if "scraper" in item]


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except TypeError, ValueError:
        return None


def _fmt_brl(value) -> str | None:
    v = _as_int(value)
    return f"R$ {v:,}".replace(",", ".") if v is not None else None


def _passes_area_per_room(item: Item, min_ratio: float) -> bool:
    """Keep listings with enough area per bedroom; pass through if unknown."""
    area = _as_int(item.meta.get("area"))
    bedrooms = _as_int(item.meta.get("bedrooms"))
    if not area or not bedrooms:
        return True
    return (area / bedrooms) >= min_ratio


def _title(listing: dict) -> str:
    parts = [listing.get("type") or "Imóvel"]
    if listing.get("bedrooms"):
        parts.append(f"{listing['bedrooms']}q")
    loc = ", ".join(s for s in (listing.get("neighborhood"), listing.get("city")) if s)
    if loc:
        parts.append(f"- {loc}")
    return " ".join(parts)


def _format_body(listing: dict) -> str:
    price = _as_int(listing.get("price"))
    condo = _as_int(listing.get("condo_fee"))
    if price is None:
        money = "Sob consulta"
    elif condo:
        money = f"{_fmt_brl(price)} + {_fmt_brl(condo)} = {_fmt_brl(price + condo)}"
    else:
        money = _fmt_brl(price)

    specs = [money]
    if listing.get("area"):
        specs.append(f"{listing['area']}m²")
    if listing.get("bedrooms"):
        specs.append(f"{listing['bedrooms']} quartos")
    if listing.get("bathrooms"):
        specs.append(f"{listing['bathrooms']} ban.")
    if listing.get("parking"):
        specs.append(f"{listing['parking']} vaga")

    lines = [" · ".join(specs)]
    if listing.get("amenities"):
        lines.append(" · ".join(listing["amenities"]))
    extra = listing.get("street") or listing.get("description")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _to_item(listing: dict, source: str) -> Item:
    title = listing.get("title") or _title(listing)
    return Item(
        id=listing["url"],
        title=title[:256],
        source=source,
        url=listing["url"],
        body=_format_body(listing),
        image=listing.get("image"),
        images=listing.get("images") or [],
        meta={k: listing.get(k) for k in _META_FIELDS},
    )


async def pull_scrapers(
    scraper_cfgs: list[dict],
    seen_per_adapter: dict[str, set[str]],
) -> dict[str, PullResult | None]:
    """Fetch all configured scrapers via vasco's realestate adapter.

    Returns `{adapter_id: PullResult | None}`. A `None` value means that source
    failed (missing url/adapter id, or fetch error) and the caller must
    preserve its prior state. Sources without a `seen` entry get an empty set.
    """
    results: dict[str, PullResult | None] = {}

    valid: list[tuple[str, dict]] = []
    for sc in scraper_cfgs:
        adapter = sc.get("adapter", "")
        if not adapter:
            log.error("[scraper] scraper config missing `adapter` id: %s", sc.get("url"))
            continue
        if not sc.get("url"):
            log.error("[scraper] No url configured for %r", adapter)
            results[adapter] = None
            continue
        valid.append((adapter, sc))

    if not valid:
        return results

    envelopes = await asyncio.gather(*[fetch_listings(sc["url"]) for _, sc in valid])

    for (adapter, sc), env in zip(valid, envelopes):
        url = sc["url"]
        max_items = sc.get("max_items")
        min_ratio = sc.get("min_area_per_room")
        seen = seen_per_adapter.get(adapter, set())
        log.info("[scraper] %s → %s", adapter, url)

        if env is None:
            log.error("[scraper] %s: failed to fetch %s", adapter, url)
            results[adapter] = None
            continue

        source = env.get("site_name") or sc.get("name") or adapter
        listings = (env.get("quality") or {}).get("listings") or []
        items = [_to_item(ln, source) for ln in listings if ln.get("url")]

        if min_ratio is not None:
            before = len(items)
            items = [it for it in items if _passes_area_per_room(it, min_ratio)]
            log.info(
                "[scraper] %s: %d → %d after min_area_per_room=%s m²/quarto",
                adapter,
                before,
                len(items),
                min_ratio,
            )

        current = [{"url": it.id, "title": it.title} for it in items]
        new_items = [it for it in items if it.id not in seen]
        if max_items is not None:
            new_items = new_items[:max_items]
        log.info(
            "[scraper] %s: %d total, %d new (capped: %s)",
            adapter,
            len(items),
            len(new_items),
            max_items,
        )
        results[adapter] = PullResult(new_items=new_items, current_items=current)

    return results
