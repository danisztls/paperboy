import logging
import re

from playwright.async_api import Page

from pipeline import Item

from .base import SiteAdapter, register_adapter

log = logging.getLogger(__name__)

# Elementor "Loop Grid" template — every listing is rendered as a top-level
# `.imovel.type-imovel` div with two icon-list widgets inside (specs first,
# location second). No price on the listing card; that lives behind the detail
# page. Body classes (`pretensao-aluguel`, `tipo_de_imovel-apartamento`,
# `cidade-baixo-guandu`) are more stable than parsing the visible labels, so
# we read them too.
_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('.imovel.type-imovel')).map(card => {
    const detailLink = card.querySelector('a[href*="/imovel/"]');
    const titleEl = card.querySelector('h1.elementor-heading-title');
    const tagEl = card.querySelector('h2.elementor-heading-title a[rel="tag"]');
    const imgEl = card.querySelector('img');
    const widgets = card.querySelectorAll('.elementor-widget-icon-list');
    const textsOf = w => w
        ? Array.from(w.querySelectorAll('.elementor-icon-list-text')).map(s => s.textContent.trim())
        : [];
    const specs = textsOf(widgets[0]);
    const location = textsOf(widgets[1]);
    return {
        url: detailLink ? detailLink.href : null,
        title: titleEl ? titleEl.textContent.trim() : '',
        pretensao: tagEl ? tagEl.textContent.trim() : null,
        image: imgEl ? imgEl.src : null,
        specs: specs,
        location: location,
        classes: card.className.split(/\\s+/),
    };
})
"""

_TYPE_MAP = {
    "apartamento": "Apartamento",
    "casa": "Casa",
    "lote-vago": "Lote",
    "lote": "Lote",
    "residencial": "Residencial",
    "rural": "Rural",
    "urbano": "Urbano",
    "comercial": "Comercial",
}


def _imovel_type(classes: list[str]) -> str | None:
    """Pick the most specific `tipo_de_imovel-*` class (skips 'urbano'/'rural' tags)."""
    tags = [c.removeprefix("tipo_de_imovel-") for c in classes if c.startswith("tipo_de_imovel-")]
    if not tags:
        return None
    for t in tags:
        if t not in ("urbano", "rural"):
            return _TYPE_MAP.get(t, t.replace("-", " ").title())
    return _TYPE_MAP.get(tags[0], tags[0].replace("-", " ").title())


def _normalize_area(s: str) -> str:
    """'45M²m2' / '45m2' → '45m²'."""
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not m:
        return s
    return f"{m.group(1)}m²"


def _format_specs(specs: list[str]) -> str | None:
    """Spec order on the card is fixed: [beds, baths, parking, area]."""
    if not specs:
        return None
    labels = ["quarto", "ban.", "vaga", None]
    parts: list[str] = []
    for i, raw in enumerate(specs[:4]):
        if not raw:
            continue
        label = labels[i]
        if label is None:
            parts.append(_normalize_area(raw))
        elif label == "quarto":
            n = re.search(r"\d+", raw)
            if n:
                v = int(n.group())
                parts.append(f"{v} {'quarto' if v == 1 else 'quartos'}")
        elif label == "ban.":
            n = re.search(r"\d+", raw)
            if n:
                parts.append(f"{n.group()} ban.")
        elif label == "vaga":
            parts.append(raw if "moto" in raw.lower() else f"{raw} vaga")
    return " · ".join(parts) if parts else None


def _format_location(location: list[str]) -> str | None:
    parts = [s for s in location if s]
    return ", ".join(reversed(parts)) if parts else None


def _format_body(specs, location, imovel_type, pretensao) -> str:
    lines: list[str] = []
    spec_line = _format_specs(specs)
    if spec_line:
        lines.append(spec_line)
    loc = _format_location(location)
    if loc:
        lines.append(loc)
    tail = [s for s in [imovel_type, pretensao] if s]
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


@register_adapter("imoveis_barreto")
class ImoveisBarretoAdapter(SiteAdapter):
    @property
    def name(self) -> str:
        return "imoveis_barreto"

    async def scrape(self, url: str, cfg: dict, seen: set[str], page: Page) -> list[Item]:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            cards = await page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            log.error("[imoveis_barreto] DOM extraction failed: %s", exc)
            return []

        items: list[Item] = []
        for c in cards:
            url = c.get("url")
            title = c.get("title") or ""
            if not url or not title:
                continue
            imovel_type = _imovel_type(c.get("classes") or [])
            items.append(
                Item(
                    id=url,
                    title=title[:256],
                    source="Barreto Imóveis",
                    url=url,
                    body=_format_body(
                        c.get("specs") or [],
                        c.get("location") or [],
                        imovel_type,
                        c.get("pretensao"),
                    ),
                    image=c.get("image"),
                    meta={
                        "pretensao": c.get("pretensao"),
                        "type": imovel_type,
                        "specs": c.get("specs"),
                        "location": c.get("location"),
                    },
                )
            )
        log.info("[imoveis_barreto] %d listings", len(items))
        return items
