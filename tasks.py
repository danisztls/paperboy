import asyncio
import logging
import re
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import aiohttp

from config import _parse_color, _task_type
from feed import get_new_entries
from discord import post_to_discord, post_text_to_discord, post_digest_to_discord
from llm import run_llm_task, filter_entries

DEFAULT_PERIOD = timedelta(hours=1)
PERIOD_GRACE = timedelta(seconds=60)
_CITE_STRIP_RE = re.compile(r'\s*\[\d+\]')

log = logging.getLogger(__name__)


def _merge_filter(task_f: dict, feed_f: dict) -> dict:
    """Combine task-level and feed-level filter dicts, concatenating rules for shared keys."""
    merged = {}
    for key in set(task_f) | set(feed_f):
        task_val = task_f.get(key)
        feed_val = feed_f.get(key)
        if task_val is None:
            merged[key] = feed_val
        elif feed_val is None:
            merged[key] = task_val
        else:
            task_list = task_val if isinstance(task_val, list) else [task_val]
            feed_list = feed_val if isinstance(feed_val, list) else [feed_val]
            merged[key] = task_list + feed_list
    return merged



def _is_due(feed_state: dict, period: timedelta, now: datetime) -> bool:
    last_run = feed_state.get("last_run")
    if not last_run:
        return True
    try:
        last = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    return (now - last) >= period - PERIOD_GRACE


async def _process_llm_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    instructions: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
) -> dict:
    """Run one LLM task, post the response, return {name: task_state} on success or {} on failure."""
    name = task_cfg["name"]
    text = await run_llm_task(task_cfg, instructions, llm_model, api_key=llm_api_key)
    if text is None:
        return {}
    webhook = task_cfg.get("discord", {}).get("webhook")
    try:
        await post_text_to_discord(webhook, text, session)
        log.info("[%s] Posted LLM response (%d chars)", name, len(text))
    except Exception:
        log.error("Skipping LLM task %s due to post failure", name)
        return {}
    return {name: {"last_run": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}}


async def _process_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    global_color: int | None = None,
    global_language: str = "EN-US",
    global_og_download: bool = False,
) -> dict:
    """Run one RSS task (filtered or not), return {task_name: task_state}."""
    task_name = task_cfg["name"]
    task_discord = task_cfg.get("discord", {})
    webhook = task_discord.get("webhook")
    task_color = _parse_color(task_discord.get("color"))
    filter_cfg = task_cfg.get("llm") or None
    explain = bool(filter_cfg.get("explain")) if filter_cfg else False
    feed_cfgs = [fc for fc in task_cfg.get("feeds", []) if fc.get("url")]
    task_state = state.get("tasks", {}).get(task_name, {})
    feeds_state = task_state.get("feeds", {})

    # Memory history (filtered tasks only)
    raw_history = task_state.get("memory", {}) if filter_cfg else {}
    memory_history: list[str] | None = None
    if raw_history:
        keys = sorted(raw_history)[-7:]
        memory_history = [(k, raw_history[k]) for k in keys] or None

    # Fetch all feeds concurrently
    task_og_cfg = task_cfg.get("og_image") or {}
    fetch_og = not task_og_cfg.get("skip", False)
    task_og_download = task_og_cfg.get("download")
    task_filter = task_cfg.get("filter", {})

    async def _fetch_one(fc: dict):
        url = fc["url"]
        seen = {item["url"] for item in feeds_state.get(url, {}).get("items", [])}
        feed_filter = fc.get("filter", {})
        merged_filter = _merge_filter(task_filter, feed_filter) if (task_filter or feed_filter) else {}
        effective_fc = {**fc, "filter": merged_filter} if merged_filter else fc
        return fc, await get_new_entries(effective_fc, seen, session)

    fetch_results = await asyncio.gather(*[_fetch_one(fc) for fc in feed_cfgs], return_exceptions=True)

    # Build global-ID payload for LLM filter
    global_id = 0
    id_map: dict[int, object] = {}
    feed_fetch_map: dict[str, tuple | None] = {}
    payload_groups: list[dict] = []

    for item in fetch_results:
        if isinstance(item, Exception):
            log.error("[%s] Feed fetch failed: %s", task_name, item)
            continue
        fc, result = item
        url = fc["url"]
        if result is None:
            feed_fetch_map[url] = None
            continue
        current_items, new_entries = result
        feed_fetch_map[url] = (current_items, new_entries)
        if filter_cfg and new_entries:
            group = {"source": new_entries[0].feed_title, "items": []}
            for entry in new_entries:
                item_payload: dict = {"id": global_id, "title": entry.title}
                if entry.description:
                    item_payload["description"] = entry.description
                group["items"].append(item_payload)
                id_map[global_id] = entry
                global_id += 1
            payload_groups.append(group)

    # One LLM filter call for the whole task
    filter_result: dict | None = None
    memory_text: str | None = None
    cite_map: dict[int, tuple[str, str | None]] = {gid: (entry.feed_title, entry.link) for gid, entry in id_map.items()}
    if filter_cfg and payload_groups:
        language = filter_cfg.get("language") or global_language
        _filter_kwargs = dict(
            language=language,
            memory_history=memory_history,
            api_key=llm_api_key,
            extra_instructions=filter_cfg.get("instructions") or None,
        )
        llm_return = await filter_entries(payload_groups, filter_cfg, llm_model, **_filter_kwargs)
        if llm_return is None:
            log.warning("[%s] Filter failed, retrying in 10s", task_name)
            await asyncio.sleep(10)
            llm_return = await filter_entries(payload_groups, filter_cfg, llm_model, **_filter_kwargs)
        if llm_return is None:
            log.error("[%s] Filter failed twice — skipping task", task_name)
            return {}
        raw_results, memory_text = llm_return
        filter_result = {
            id_map[int(gid)].id: v
            for gid, v in raw_results.items()
            if gid.isdigit() and int(gid) in id_map
        }

    task_type = _task_type(task_cfg)
    all_entries_to_post: list = []
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new_feeds_state = dict(feeds_state)  # carry forward state for feeds not fetched this run

    for fc in feed_cfgs:
        url = fc["url"]
        fetch = feed_fetch_map.get(url)
        if fetch is None:
            continue  # failed fetch — leave existing state untouched
        current_items, new_entries = fetch

        prev_items = feeds_state.get(url, {}).get("items", [])
        prev_by_url = {item["url"]: item for item in prev_items}

        if filter_cfg:
            if filter_result is None:
                passing_ids = {e.id for e in new_entries}
            else:
                passing_ids = {eid for eid, v in filter_result.items() if v["pass"]}
            entries_to_post = [e for e in new_entries if e.id in passing_ids]
        else:
            entries_to_post = new_entries

        # Preserve all old items; append new ones not already in state
        if filter_cfg:
            new_entry_by_link = {e.link: e for e in new_entries}
            final_items = list(prev_items)
            for item in current_items:
                item_url = item["url"]
                if item_url not in prev_by_url:
                    e = new_entry_by_link.get(item_url)
                    if e is not None and filter_result is not None and e.id in filter_result:
                        v = filter_result[e.id]
                        final_items.append({**item, "filter_pass": v["pass"], "filter_reason": v["reason"]})
                    else:
                        final_items.append({**item, "filter_pass": True})
        else:
            final_items = list(prev_items) + [item for item in current_items if item["url"] not in prev_by_url]

        if task_type != "digest":
            feed_og_download = (fc.get("og_image") or {}).get("download")
            download_og = (
                feed_og_download if feed_og_download is not None
                else task_og_download if task_og_download is not None
                else global_og_download
            )
            feed_color = _parse_color(fc.get("discord", {}).get("color")) or task_color or global_color
            for e in entries_to_post:
                if explain and filter_result is not None:
                    v = filter_result.get(e.id)
                    if v and v.get("reason"):
                        e = dc_replace(e, description=v["reason"])
                all_entries_to_post.append((feed_color, download_og, e))

        for item in final_items:
            if "access_date" not in item:
                item["access_date"] = now_iso

        new_feeds_state[url] = {"items": final_items, "last_run": now_iso}

    failed_links: set[str] = set()
    if task_type != "digest" and all_entries_to_post:
        _far_future = datetime.max.replace(tzinfo=timezone.utc)
        all_entries_to_post.sort(key=lambda c_e: c_e[2].published or _far_future)
        for i, (entry_color, entry_download_og, entry) in enumerate(all_entries_to_post):
            try:
                await post_to_discord(webhook, entry, session, fetch_og=fetch_og, download_og=entry_download_og, color=entry_color)
                log.info("[%s] Posted: %s", entry.feed_title, entry.title[:80])
                if i < len(all_entries_to_post) - 1:
                    await asyncio.sleep(2)
            except Exception:
                log.error("Skipping entry %s due to post failure", entry.id)
                if entry.link:
                    failed_links.add(entry.link)

    if failed_links:
        for feed_state in new_feeds_state.values():
            feed_state["items"] = [item for item in feed_state["items"] if item["url"] not in failed_links]

    if task_type == "digest" and memory_text:
        try:
            await post_digest_to_discord(webhook, session, memory_text=memory_text, cite_map=cite_map)
            log.info("[%s] Posted digest", task_name)
        except Exception:
            log.error("[%s] Failed to post digest — state not saved", task_name)
            return {}

    new_task_state: dict = {"feeds": new_feeds_state}
    if filter_cfg:
        history = dict(raw_history)
        if memory_text is not None:
            _stripped = _CITE_STRIP_RE.sub('', memory_text)
            history[now_iso] = " ".join(line.strip() for line in _stripped.splitlines() if line.strip())
            if len(history) > 20:
                for old_key in sorted(history)[:len(history) - 20]:
                    del history[old_key]
            log.info("[%s] Memory updated (%d chars)", task_name, len(memory_text))
        new_task_state["memory"] = history

    return {task_name: new_task_state}
