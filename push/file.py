import logging
import os
import pathlib
import re
from datetime import UTC, datetime

from config import get_file_path
from pipeline import Item, PushContext, Target

log = logging.getLogger(__name__)

_CITE_RE = re.compile(r"\[(\d+)\]")
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _resolve_path(path_str: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(path_str)))


def _apply_cite_map_md(text: str, cite_map: dict[int, tuple[str, str | None]]) -> str:
    def replace(m: re.Match) -> str:
        item = cite_map.get(int(m.group(1)))
        if item is None:
            return m.group(0)
        name, url = item
        return f"[{name}]({url})" if url else f"[{name}]"

    return _CITE_RE.sub(replace, text)


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
        text = _apply_cite_map_md(ctx.memory, ctx.cite_map) if ctx.cite_map else ctx.memory

        block = f"## {date_str}\n\n{text.strip()}\n\n---\n\n"

        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(block)
            log.info("Appended digest to %s", path)
        except OSError as exc:
            log.error("Failed to write to %s: %s", path, exc)

        return set()
