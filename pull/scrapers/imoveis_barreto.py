import logging
import re

from bs4 import BeautifulSoup

from pipeline import Item

from .base import SiteAdapter, register_adapter

log = logging.getLogger(__name__)

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
    tags = [c.removeprefix("tipo_de_imovel-") for c in classes if c.startswith("tipo_de_imovel-")]
    if not tags:
        return None
    for t in tags:
        if t not in ("urbano", "rural"):
            return _TYPE_MAP.get(t, t.replace("-", " ").title())
    return _TYPE_MAP.get(tags[0], tags[0].replace("-", " ").title())


def _normalize_area(s: str) -> str:
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not m:
        return s
    return f"{m.group(1)}m²"


def _format_specs(specs: list[str]) -> str | None:
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

    async def scrape(self, url: str, cfg: dict, seen: set[str], html: str) -> list[Item]:
        soup = BeautifulSoup(html, "html.parser")

        items: list[Item] = []
        for card in soup.select(".imovel.type-imovel"):
            detail_link = card.select_one('a[href*="/imovel/"]')
            title_el = card.select_one("h1.elementor-heading-title")
            tag_el = card.select_one('h2.elementor-heading-title a[rel="tag"]')
            img_el = card.select_one("img")

            widgets = card.select(".elementor-widget-icon-list")
            specs = [
                el.get_text(strip=True)
                for el in (
                    widgets[0].select(".elementor-icon-list-text") if len(widgets) > 0 else []
                )
            ]
            location = [
                el.get_text(strip=True)
                for el in (
                    widgets[1].select(".elementor-icon-list-text") if len(widgets) > 1 else []
                )
            ]

            item_url = detail_link["href"] if detail_link and detail_link.get("href") else None
            title = title_el.get_text(strip=True) if title_el else ""
            if not item_url or not title:
                continue

            classes = card.get("class", [])
            imovel_type = _imovel_type(classes)
            pretensao = tag_el.get_text(strip=True) if tag_el else None
            image = img_el.get("src") if img_el else None

            items.append(
                Item(
                    id=item_url,
                    title=title[:256],
                    source="Barreto Imóveis",
                    url=item_url,
                    body=_format_body(
                        specs,
                        location,
                        imovel_type,
                        pretensao,
                    ),
                    image=image,
                    meta={
                        "pretensao": pretensao,
                        "type": imovel_type,
                        "specs": specs,
                        "location": location,
                    },
                )
            )
        log.info("[imoveis_barreto] %d listings", len(items))
        return items
