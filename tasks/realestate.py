"""Real-estate task: structured listings from portals via vasco's realestate adapter."""

import logging
from dataclasses import replace as dc_replace

from config import get_discord_cfg, get_file_path, get_realestate_cfgs, parse_color
from config.scope import layer_dict
from pipeline import Item, PushContext
from pull.realestate import pull_realestate
from push.discord import DiscordEmbedTarget, DiscordMarkdownTarget
from push.file import FileItemTarget
from tasks.context import RunContext
from util import utc_now_iso

log = logging.getLogger(__name__)


async def process_realestate_task(task_cfg: dict, state: dict, ctx: RunContext) -> dict:
    """Scrape one or more sites, post new listings as one batched Discord stream.

    State is per-url (`realestate[<url>]: {items, last_run}`). A failed source
    preserves its prior state; task-level `last_run` advances if at least one
    source succeeded. The `__legacy__` bucket (from the v3→v4 migration)
    contributes URLs to every source's `seen` set for dedup but is never
    written to.
    """
    task_name = task_cfg["name"]
    task_discord = get_discord_cfg(task_cfg)
    task_color = parse_color(layer_dict(ctx.config.get("discord"), task_discord).get("color"))

    task_state = state.get("tasks", {}).get(task_name, {})
    realestate_state = task_state.get("realestate", {})
    legacy_seen = {
        it["url"] for it in realestate_state.get("__legacy__", {}).get("items", []) if "url" in it
    }

    realestate_cfgs = get_realestate_cfgs(task_cfg)
    seen_per_url: dict[str, set[str]] = {}
    for sc in realestate_cfgs:
        url = sc.get("url")
        if not url:
            continue
        prev = realestate_state.get(url, {}).get("items", [])
        seen_per_url[url] = {it["url"] for it in prev} | legacy_seen

    results = await pull_realestate(realestate_cfgs, seen_per_url)
    if not results or all(r is None for r in results.values()):
        return {}

    now_iso = utc_now_iso()

    all_new_items: list[Item] = []
    for result in results.values():
        if result is None:
            continue
        for it in result.new_items:
            all_new_items.append(dc_replace(it, meta={**it.meta, "color": task_color}))

    failed_ids: set[str] = set()
    if all_new_items:
        push_ctx = PushContext(items=all_new_items)
        if task_discord.get("format") == "markdown":
            failed_ids = await DiscordMarkdownTarget().push(push_ctx, task_cfg, ctx.session)
        else:
            failed_ids = await DiscordEmbedTarget().push(push_ctx, task_cfg, ctx.session)
        posted = len(all_new_items) - len(failed_ids)
        if posted > 0:
            log.info("[%s] Posted %d listing(s) to Discord", task_name, posted)
        if get_file_path(task_cfg):
            await FileItemTarget().push(push_ctx, task_cfg, ctx.session)

    new_realestate_state = dict(realestate_state)
    for url, result in results.items():
        if result is None:
            continue
        prev_items = realestate_state.get(url, {}).get("items", [])
        prev_by_url = {it["url"]: it for it in prev_items}
        merged = list(prev_items)
        for ci in result.current_items:
            if ci["url"] not in prev_by_url:
                merged.append({**ci, "first_seen": now_iso})
        if failed_ids:
            merged = [it for it in merged if it["url"] not in failed_ids]
        new_realestate_state[url] = {"items": merged, "last_run": now_iso}

    return {task_name: {**task_state, "realestate": new_realestate_state, "last_run": now_iso}}
