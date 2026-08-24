# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests for the file push target: JSONL output + extension validation."""

import json
from datetime import UTC, datetime

import aiohttp

from config import validate_config
from pipeline import Citation, Item, MemoryParagraph, PushContext
from push.file import FileDigestTarget, FileItemTarget


def _base_task_cfg(file_path: str) -> dict:
    return {
        "name": "t",
        "pull": [{"feed": {"url": "https://example.com/feed.xml"}}],
        "push": [{"file": file_path}],
    }


def test_validate_config_accepts_md_and_jsonl(tmp_path):
    assert validate_config({"tasks": [_base_task_cfg(str(tmp_path / "out.md"))]}) == []
    assert validate_config({"tasks": [_base_task_cfg(str(tmp_path / "out.jsonl"))]}) == []


def test_validate_config_rejects_other_extensions(tmp_path):
    for bad in ("out.txt", "out.json", "out", "out.csv"):
        errors = validate_config({"tasks": [_base_task_cfg(str(tmp_path / bad))]})
        assert errors, f"expected validation error for {bad}"
        assert any(".md or .jsonl" in e for e in errors)


def _item(idx: int) -> Item:
    return Item(
        id=f"id-{idx}",
        title=f"Title {idx}",
        source="Example",
        url=f"https://example.com/posts/{idx}",
        body=f"Body of item {idx}.",
        published=datetime(2026, 5, 19, 12, idx, 0, tzinfo=UTC),
    )


async def test_file_item_target_writes_jsonl(tmp_path):
    out_file = tmp_path / "items.jsonl"
    items = [_item(1), _item(2)]
    ctx = PushContext(items=items)
    cfg = {"push": [{"file": str(out_file)}]}

    async with aiohttp.ClientSession() as session:
        failed = await FileItemTarget().push(ctx, cfg, session)

    assert failed == set()
    lines = out_file.read_text().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    for rec, item in zip(records, items, strict=True):
        assert rec["id"] == item.id
        assert rec["title"] == item.title
        assert rec["url"] == item.url
        assert rec["body"] == item.body
        assert rec["published"] == item.published.isoformat()
        # No None fields leaked through.
        assert "image" not in rec
        assert "summary" not in rec
        assert "filter_pass" not in rec
        # Empty meta dict dropped.
        assert "meta" not in rec


async def test_file_item_target_jsonl_appends_on_second_run(tmp_path):
    out_file = tmp_path / "items.jsonl"
    cfg = {"push": [{"file": str(out_file)}]}

    async with aiohttp.ClientSession() as session:
        await FileItemTarget().push(PushContext(items=[_item(1)]), cfg, session)
        await FileItemTarget().push(PushContext(items=[_item(2), _item(3)]), cfg, session)

    lines = out_file.read_text().splitlines()
    assert len(lines) == 3
    ids = [json.loads(line)["id"] for line in lines]
    assert ids == ["id-1", "id-2", "id-3"]


async def test_file_digest_target_writes_jsonl(tmp_path):
    out_file = tmp_path / "digest.jsonl"
    memory = [
        MemoryParagraph(text="Cat update.", citations=[0]),
        MemoryParagraph(text="Quantum advance.", citations=[1], section="Science"),
    ]
    cite_map = {
        0: Citation(source="Example", url="https://example.com/posts/1"),
        1: Citation(source="Example", url="https://example.com/posts/2"),
    }
    ctx = PushContext(items=[], memory=memory, cite_map=cite_map)
    cfg = {"push": [{"file": str(out_file)}]}

    async with aiohttp.ClientSession() as session:
        await FileDigestTarget().push(ctx, cfg, session)
        await FileDigestTarget().push(ctx, cfg, session)

    lines = out_file.read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert "date" in rec
    paras = rec["paragraphs"]
    assert paras[0]["text"] == "Cat update."
    assert "section" not in paras[0]  # absent, not null
    assert paras[0]["citations"] == [{"source": "Example", "url": "https://example.com/posts/1"}]
    assert paras[1]["section"] == "Science"
    assert paras[1]["citations"] == [{"source": "Example", "url": "https://example.com/posts/2"}]
