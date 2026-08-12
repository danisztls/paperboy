"""Web scraping source: structured real-estate listings via vasco.

vasco's realestate adapter (``mode_used="realestate"``) parses the source portal
(vivareal) into normalized listing dicts; this module maps
them to pipeline ``Item``s and applies paperboy-side policy
(``min_area_per_room`` filter, ``max_items`` cap, ``seen`` dedup).

vasco picks the parser by domain, so a config item just needs a ``url`` —
which also serves as the per-source identity used to key state.
"""

import asyncio
import logging
import unicodedata
from urllib.parse import urljoin

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


def _normalize(s: str) -> str:
    """Casefold + strip accents for tolerant neighborhood matching."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).casefold().strip()


def _passes_neighborhood(item: Item, excluded: list[str]) -> bool:
    """Drop listings whose neighborhood matches an excluded entry; pass through if unknown."""
    raw = item.meta.get("neighborhood")
    if not raw:
        return True  # field absent for this source → don't filter
    n = _normalize(str(raw))
    return not any(_normalize(e) in n or n in _normalize(e) for e in excluded)


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


def _abs_url(base: str, u: str | None) -> str | None:
    """Resolve a possibly-relative image URL against the listing page URL.

    A portal may emit relative image paths like
    `../imoveis/x.jpeg`; Discord rejects non-absolute embed image URLs with a 400,
    so absolutize here at the source→Item boundary.
    """
    if not isinstance(u, str) or not u.strip():
        return None
    return urljoin(base, u.strip())


def _to_item(listing: dict, source: str) -> Item:
    title = listing.get("title") or _title(listing)
    url = listing["url"]
    images = [a for img in (listing.get("images") or []) if (a := _abs_url(url, img))]
    image = _abs_url(url, listing.get("image")) or (images[0] if images else None)
    return Item(
        id=url,
        title=title[:256],
        source=source,
        url=url,
        body=_format_body(listing),
        image=image,
        images=images,
        meta={k: listing.get(k) for k in _META_FIELDS},
    )


async def pull_realestate(
    realestate_cfgs: list[dict],
    seen_per_url: dict[str, set[str]],
) -> dict[str, PullResult | None]:
    """Fetch all configured real-estate sources via vasco's realestate adapter.

    Returns `{url: PullResult | None}`. A `None` value means that source failed
    (fetch error) and the caller must preserve its prior state. Sources without
    a `seen` entry get an empty set.
    """
    results: dict[str, PullResult | None] = {}

    valid: list[dict] = []
    for sc in realestate_cfgs:
        if not sc.get("url"):
            log.error("[realestate] config missing `url`: %r", sc.get("name"))
            continue
        valid.append(sc)

    if not valid:
        return results

    envelopes = await asyncio.gather(*[fetch_listings(sc["url"]) for sc in valid])

    for sc, env in zip(valid, envelopes):
        url = sc["url"]
        label = sc.get("name") or url
        max_items = sc.get("max_items")
        min_ratio = sc.get("min_area_per_room")
        exclude_neighborhoods = sc.get("exclude_neighborhoods")
        seen = seen_per_url.get(url, set())
        log.debug("[realestate] %s → %s", label, url)

        if env is None:
            log.error("[realestate] %s: failed to fetch %s", label, url)
            results[url] = None
            continue

        source = env.get("site_name") or sc.get("name") or url
        listings = (env.get("quality") or {}).get("listings") or []
        items = [_to_item(ln, source) for ln in listings if ln.get("url")]

        if min_ratio is not None:
            before = len(items)
            items = [it for it in items if _passes_area_per_room(it, min_ratio)]
            log.debug(
                "[realestate] %s: %d → %d after min_area_per_room=%s m²/quarto",
                label,
                before,
                len(items),
                min_ratio,
            )

        if exclude_neighborhoods:
            before = len(items)
            items = [it for it in items if _passes_neighborhood(it, exclude_neighborhoods)]
            log.debug(
                "[realestate] %s: %d → %d after exclude_neighborhoods=%s",
                label,
                before,
                len(items),
                exclude_neighborhoods,
            )

        current = [{"url": it.id, "title": it.title} for it in items]
        new_items = [it for it in items if it.id not in seen]
        if max_items is not None:
            new_items = new_items[:max_items]
        log.debug(
            "[realestate] %s: %d total, %d new (capped: %s)",
            label,
            len(items),
            len(new_items),
            max_items,
        )
        results[url] = PullResult(new_items=new_items, current_items=current)

    return results
