import logging
import os
import pathlib
from datetime import UTC, datetime

from config import get_file_path
from pipeline import Citation, Item, MemoryParagraph, PushContext, Target

log = logging.getLogger(__name__)

_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _resolve_path(path_str: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(path_str)))


def _render_paragraph_md(para: MemoryParagraph, cite_map: dict[int, Citation] | None) -> str:
    text = para.text.rstrip()
    if para.citations and cite_map:
        links = []
        for id_ in para.citations:
            cit = cite_map.get(id_)
            if cit:
                links.append(f"[{cit.source}]({cit.url})" if cit.url else f"[{cit.source}]")
        if links:
            text += " " + " ".join(links)
    return text


def _item_to_markdown(item: Item) -> str:
    title_part = f"[{item.title}]({item.url})" if item.url else (item.title or "Untitled")
    date_part = item.published.strftime("%Y-%m-%d") if item.published else None
    meta_parts = [p for p in [item.source, date_part] if p]

    lines = [f"## {title_part}"]
    if meta_parts:
        lines.append(f"*{' · '.join(meta_parts)}*")
    if item.body:
        lines.append("")
        lines.append(item.body)
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


class FileEmbedTarget(Target):
    """Appends each item as a markdown section to a file."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        path_str = get_file_path(cfg)
        if not path_str or not ctx.items:
            return set()

        path = _resolve_path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        items = sorted(ctx.items, key=lambda e: e.published or _FAR_FUTURE)
        content = "\n".join(_item_to_markdown(item) for item in items)

        failed: set[str] = set()
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(content)
            log.info("Appended %d item(s) to %s", len(items), path)
        except OSError as exc:
            log.error("Failed to write to %s: %s", path, exc)
            failed = {item.url for item in items if item.url}

        return failed


class FileDigestTarget(Target):
    """Appends the memory digest as a dated markdown section to a file."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        path_str = get_file_path(cfg)
        if not path_str or not ctx.memory:
            return set()

        path = _resolve_path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        paragraphs = [_render_paragraph_md(p, ctx.cite_map) for p in ctx.memory if p.text.strip()]
        text = "\n\n".join(paragraphs)

        block = f"## {date_str}\n\n{text}\n\n---\n\n"

        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(block)
            log.info("Appended digest to %s", path)
        except OSError as exc:
            log.error("Failed to write to %s: %s", path, exc)

        return set()
