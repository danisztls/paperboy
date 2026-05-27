import json
import logging
import os
import pathlib
from dataclasses import asdict
from datetime import UTC, datetime

from config import get_file_path
from pipeline import Citation, Item, MemoryParagraph, PushContext, Target

log = logging.getLogger(__name__)

_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _resolve_path(path_str: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(path_str)))


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _serialize_item(item: Item) -> dict:
    raw = asdict(item)
    if item.published is not None:
        raw["published"] = item.published.isoformat()
    if not raw.get("meta"):
        raw.pop("meta", None)
    return _drop_none(raw)


def _serialize_digest(memory: list[MemoryParagraph], cite_map: dict[int, Citation] | None) -> dict:
    paragraphs: list[dict] = []
    for para in memory or []:
        if not para.text.strip():
            continue
        entry: dict = {"text": para.text.rstrip()}
        if para.section:
            entry["section"] = para.section
        if para.citations and cite_map:
            cites = [
                _drop_none({"source": c.source, "url": c.url})
                for id_ in para.citations
                if (c := cite_map.get(id_))
            ]
            if cites:
                entry["citations"] = cites
        paragraphs.append(entry)
    return {"date": datetime.now(UTC).isoformat(), "paragraphs": paragraphs}


def _append_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")


def _render_paragraph_md(para: MemoryParagraph, cite_map: dict[int, Citation] | None) -> str:
    text = para.text.rstrip()
    if para.citations and cite_map:
        links = [
            f"[{cit.source}]({cit.url})" if cit.url else f"[{cit.source}]"
            for id_ in para.citations
            if (cit := cite_map.get(id_))
        ]
        if links:
            text += " " + " ".join(links)
    if para.section:
        return f"### {para.section}\n\n{text}"
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


class FileItemTarget(Target):
    """Appends each item to a file. Format chosen by path extension."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        path_str = get_file_path(cfg)
        if not path_str or not ctx.items:
            return set()

        path = _resolve_path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        ext = path.suffix.lower()

        items = sorted(ctx.items, key=lambda e: e.published or _FAR_FUTURE)

        failed: set[str] = set()
        try:
            if ext == ".jsonl":
                _append_jsonl(path, [_serialize_item(item) for item in items])
            else:
                content = "\n".join(_item_to_markdown(item) for item in items)
                with path.open("a", encoding="utf-8") as f:
                    f.write(content)
            log.info("Appended %d item(s) to %s", len(items), path)
        except OSError as exc:
            log.error("Failed to write to %s: %s", path, exc)
            failed = {item.url for item in items if item.url}

        return failed


class FileDigestTarget(Target):
    """Appends the memory digest to a file. Format chosen by path extension."""

    async def push(self, ctx: PushContext, cfg: dict, session) -> set[str]:
        path_str = get_file_path(cfg)
        if not path_str or not ctx.memory:
            return set()

        path = _resolve_path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        ext = path.suffix.lower()

        try:
            if ext == ".jsonl":
                _append_jsonl(path, [_serialize_digest(ctx.memory, ctx.cite_map)])
            else:
                date_str = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
                paragraphs = [
                    _render_paragraph_md(p, ctx.cite_map) for p in ctx.memory if p.text.strip()
                ]
                text = "\n\n".join(paragraphs)
                block = f"## {date_str}\n\n{text}\n\n---\n\n"
                with path.open("a", encoding="utf-8") as f:
                    f.write(block)
            log.info("Appended digest to %s", path)
        except OSError as exc:
            log.error("Failed to write to %s: %s", path, exc)

        return set()
