import asyncio
import logging
import re
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta

import aiohttp

from config import _get_discord_cfg, _get_feeds, _get_file_path, _parse_color, _task_type
from llm.adapters.base import LLMAdapter
from llm_filter import filter_entries
from pipeline import FilterResult, Item, PushContext
from pull.feed import RSSSource
from pull.llm import LLMSearchSource, run_llm_task
from pull.scraper import ScraperSource
from push.discord import (
    DiscordDigestTarget,
    DiscordEmbedTarget,
    DiscordMarkdownTarget,
    DiscordTextTarget,
)
from push.file import FileDigestTarget, FileEmbedTarget
from summarize import fetch_item_content, summarize_entry

DEFAULT_PERIOD = timedelta(hours=1)
PERIOD_GRACE = timedelta(seconds=60)
_CITE_STRIP_RE = re.compile(r"\s*\[\d+\]")

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


async def _pull_feeds(
    source: RSSSource,
    feed_cfgs: list[dict],
    feeds_state: dict,
    task_filter: dict,
    session: aiohttp.ClientSession,
    *,
    collector=None,
) -> tuple[dict[str, object], dict[str, dict]]:
    """Fetch all feeds concurrently. Returns ({url: PullResult | None}, {url: filter_log})."""
    task_filter = task_filter or {}

    async def _fetch_one(fc: dict):
        url = fc["url"]
        seen = (
            set()
            if collector
            else {item["url"] for item in feeds_state.get(url, {}).get("items", [])}
        )
        feed_filter = fc.get("filter", {})
        merged_filter = (
            _merge_filter(task_filter, feed_filter) if (task_filter or feed_filter) else {}
        )
        effective_fc = {**fc, "filter": merged_filter} if merged_filter else fc
        filter_log = (
            {
                "url_excluded": [],
                "title_transforms": [],
                "description_transforms": [],
                "total_in_feed": 0,
                "new_eligible": 0,
            }
            if collector
            else None
        )
        return fc, await source.pull(effective_fc, seen, session, filter_log=filter_log), filter_log

    results = await asyncio.gather(*[_fetch_one(fc) for fc in feed_cfgs], return_exceptions=True)

    fetch_map: dict[str, object] = {}
    filter_log_map: dict[str, dict] = {}
    for item in results:
        if isinstance(item, Exception):
            log.error("Feed fetch failed: %s", item)
            continue
        fc, pull_result, filter_log = item
        fetch_map[fc["url"]] = pull_result
        if filter_log is not None:
            filter_log_map[fc["url"]] = filter_log

    return fetch_map, filter_log_map


async def _summarize_items(
    items: list[Item],
    cfg_by_id: dict[str, tuple[str, str | None]],
    llm_adapter: LLMAdapter | None,
    model: str | None,
    session: aiohttp.ClientSession | None = None,
    *,
    collector=None,
) -> list[Item]:
    """Replace .summary on items that have fetchable content or a body, concurrently."""

    async def _get_content(e: Item) -> str:
        if session:
            fetched = await fetch_item_content(e.url, session)
            if fetched:
                return fetched
        return e.body

    if llm_adapter is None:
        log.error("Summarize skipped — llm.models.reasoning is not configured")
        return items

    async def _fetch_and_summarize(e: Item) -> tuple[Item, str | None, str | None, dict | None]:
        content = await _get_content(e)
        if not content:
            return e, None, None, None
        cap: dict | None = {} if collector else None
        try:
            summary = await summarize_entry(
                e.title,
                content,
                llm_adapter,
                model=model,
                language=cfg_by_id[e.id][0],
                instructions=cfg_by_id[e.id][1],
                capture=cap,
            )
        except Exception as exc:
            log.error("summarize_entry failed for %s: %s", e.url, exc)
            summary = None
        return e, content, summary, cap

    results = await asyncio.gather(*[_fetch_and_summarize(e) for e in items])

    updated: dict[str, Item] = {}
    for e, content, summary, cap in results:
        if summary:
            updated[e.id] = dc_replace(e, summary=summary)
        if collector and cap is not None:
            collector.record_summarization(
                item_id=e.id,
                title=e.title,
                url=e.url,
                fetched_body=content,
                instructions=cap.get("instructions", ""),
                input_text=cap.get("input", ""),
                summary=summary,
            )
    return [updated.get(e.id, e) for e in items]


async def _apply_llm_filter(
    all_items: list[Item],
    filter_cfg: dict,
    model: str | None,
    language: str,
    memory_history: list[tuple[str, str]] | None,
    llm_adapter: LLMAdapter | None,
    *,
    collector=None,
) -> FilterResult:
    """Run LLM filter on items. Returns FilterResult with filter_pass set on each item."""
    # Build grouped payload for the LLM (grouped by source)
    global_id = 0
    id_map: dict[int, Item] = {}
    payload_groups: list[dict] = []

    # Group by source, preserving order
    seen_sources: dict[str, list] = {}
    for item in all_items:
        seen_sources.setdefault(item.source, []).append(item)

    for source_name, items in seen_sources.items():
        group: dict = {"source": source_name, "items": []}
        for item in items:
            payload_item: dict = {"id": global_id, "title": item.title}
            desc = item.summary or item.body
            if desc:
                payload_item["description"] = desc
            group["items"].append(payload_item)
            id_map[global_id] = item
            global_id += 1
        payload_groups.append(group)

    cite_map: dict[int, tuple[str, str | None]] = {
        gid: (item.source, item.url) for gid, item in id_map.items()
    }

    if not payload_groups:
        return FilterResult(items=all_items, memory=None, cite_map=cite_map)

    if llm_adapter is None:
        log.error("LLM filter skipped — llm.models.reasoning is not configured")
        return FilterResult(items=all_items, memory=None, cite_map=cite_map)

    filter_capture: dict | None = {} if collector else None
    _filter_kwargs = dict(
        language=language,
        memory_history=memory_history,
        adapter=llm_adapter,
        extra_instructions=filter_cfg.get("instructions") or None,
        capture=filter_capture,
    )
    llm_return = await filter_entries(payload_groups, filter_cfg, model, **_filter_kwargs)
    if llm_return is None:
        log.warning("Filter failed, retrying in 10s")
        await asyncio.sleep(10)
        llm_return = await filter_entries(payload_groups, filter_cfg, model, **_filter_kwargs)
    if llm_return is None:
        if collector and filter_capture is not None:
            collector.record_filter(
                model=model,
                instructions=filter_capture.get("instructions", ""),
                payload=filter_capture.get("payload", []),
                raw_response=filter_capture.get("raw_response"),
                parsed=[],
                memory=None,
            )
        log.error("Filter failed twice — treating all items as passing")
        return FilterResult(items=all_items, memory=None, cite_map=cite_map)

    raw_results, memory_text = llm_return

    if collector and filter_capture is not None:
        parsed_list = []
        for gid_str, v in raw_results.items():
            try:
                gid_int = int(gid_str)
                it = id_map.get(gid_int)
            except ValueError:
                it = None
            parsed_list.append(
                {
                    "id": gid_str,
                    "source": it.source if it else "?",
                    "title": it.title if it else "?",
                    "pass": v["pass"],
                    "reason": v["reason"],
                }
            )
        collector.record_filter(
            model=model,
            instructions=filter_capture.get("instructions", ""),
            payload=filter_capture.get("payload", []),
            raw_response=filter_capture.get("raw_response"),
            parsed=parsed_list,
            memory=memory_text,
        )

    # Map LLM results back to Items
    result_by_item_id: dict[str, dict] = {
        id_map[int(gid)].id: v
        for gid, v in raw_results.items()
        if gid.isdigit() and int(gid) in id_map
    }

    annotated = [
        dc_replace(
            item,
            filter_pass=result_by_item_id[item.id]["pass"]
            if item.id in result_by_item_id
            else True,
            filter_reason=result_by_item_id[item.id]["reason"]
            if item.id in result_by_item_id
            else "",
        )
        for item in all_items
    ]

    return FilterResult(items=annotated, memory=memory_text, cite_map=cite_map)


async def _process_llm_search_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    instructions: str | None = None,
    search_model: str | None = None,
    llm_adapter: LLMAdapter | None,
    collector=None,
) -> dict:
    """Pull from LLM web search, post as plain text. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    if collector:
        collector.begin_task(name, "llm_search")
    try:
        if llm_adapter is None:
            log.error("[%s] Skipping — llm.models.topic is not configured", name)
            return {}

        if collector:
            cap: dict = {}
            text = await run_llm_task(
                task_cfg, instructions, search_model, adapter=llm_adapter, capture=cap
            )
            collector.record_llm_search(
                model=cap.get("model"),
                instructions=cap.get("instructions"),
                prompt=cap.get("prompt", ""),
                raw_response=text,
            )
            new_items = (
                [Item(id=f"{name}:llm_result", title=name, source=name, body=text)] if text else []
            )
            collector.record_push(len(new_items))
            return {}

        source = LLMSearchSource(
            instructions=instructions, global_model=search_model, adapter=llm_adapter
        )
        pull_result = await source.pull(task_cfg, set(), session)
        if pull_result is None or not pull_result.new_items:
            return {}

        target = DiscordTextTarget()
        ctx = PushContext(items=pull_result.new_items)
        try:
            await target.push(ctx, task_cfg, session)
        except Exception:
            log.error("Skipping LLM task %s due to post failure", name)
            return {}

        if _get_file_path(task_cfg):
            await FileEmbedTarget().push(ctx, task_cfg, session)

        return {name: {"last_run": datetime.now(UTC).replace(microsecond=0).isoformat()}}
    finally:
        if collector:
            collector.finish_task()


async def _process_llm_evaluate_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    evaluate_model: str | None = None,
    llm_adapter: LLMAdapter | None,
    global_color: int | None = None,
    global_language: str = "EN-US",
    global_image_download: bool = False,
    collector=None,
) -> dict:
    """Pull RSS feeds, optionally filter/summarize, push to Discord. Returns {task_name: task_state}."""
    task_name = task_cfg["name"]
    task_discord = _get_discord_cfg(task_cfg)
    task_color = _parse_color(task_discord.get("color"))
    filter_cfg = task_cfg.get("llm") or None
    explain = bool(filter_cfg.get("explain")) if filter_cfg else False
    task_image_cfg = task_cfg.get("image") or {}
    fetch_image = not task_image_cfg.get("skip", False)
    task_image_download = task_image_cfg.get("download")
    task_filter = task_cfg.get("filter", {})
    task_type = _task_type(task_cfg)
    task_summarize = task_cfg.get("summarize", task_type == "digest")

    if collector:
        collector.begin_task(task_name, task_type)

    try:
        feed_cfgs = [fc for fc in _get_feeds(task_cfg) if fc.get("url")]
        if collector and collector.limit_feeds > 0:
            feed_cfgs = feed_cfgs[: collector.limit_feeds]
        task_state = state.get("tasks", {}).get(task_name, {})
        feeds_state = task_state.get("feeds", {})

        # Memory history (filtered tasks only)
        raw_history = task_state.get("memory", {}) if filter_cfg else {}
        memory_history: list[tuple[str, str]] | None = None
        if raw_history:
            keys = sorted(raw_history)[-5:]
            memory_history = [(k, raw_history[k]) for k in keys] or None

        # --- Pull ---
        source = RSSSource()
        fetch_map, filter_log_map = await _pull_feeds(
            source, feed_cfgs, feeds_state, task_filter, session, collector=collector
        )

        # Collect all new items, tagged with per-feed display metadata
        items_per_feed: dict[str, list[Item]] = {}
        all_new_items: list[Item] = []
        for fc in feed_cfgs:
            url = fc["url"]
            pull_result = fetch_map.get(url)
            if pull_result is None:
                items_per_feed[url] = []
                continue
            feed_items = pull_result.new_items
            if collector and collector.limit > 0:
                feed_items = feed_items[-collector.limit :]
            items_per_feed[url] = feed_items

            if collector:
                fl = filter_log_map.get(url, {})
                collector.record_feed(
                    url=url,
                    name=fc.get("name") or url,
                    total_in_feed=fl.get("total_in_feed", 0),
                    new_eligible=fl.get("new_eligible", 0),
                    after_limit=len(feed_items),
                    url_excluded=fl.get("url_excluded", []),
                    title_transforms=fl.get("title_transforms", []),
                    description_transforms=fl.get("description_transforms", []),
                )

            feed_image_download = (fc.get("image") or {}).get("download")
            download_image = (
                feed_image_download
                if feed_image_download is not None
                else task_image_download
                if task_image_download is not None
                else global_image_download
            )
            feed_color = (
                _parse_color(fc.get("discord", {}).get("color")) or task_color or global_color
            )
            all_new_items.extend(
                [
                    dc_replace(
                        item,
                        meta={**item.meta, "color": feed_color, "download_image": download_image},
                    )
                    for item in feed_items
                ]
            )

        # --- Process: summarize ---
        if all_new_items:
            # Build per-item summarize config: id -> (language, instructions)
            summarize_cfg_by_id: dict[str, tuple[str, str | None]] = {}
            for fc in feed_cfgs:
                url = fc["url"]
                if fetch_map.get(url) is None:
                    continue
                feed_summarize = fc.get("summarize")
                active = feed_summarize if feed_summarize is not None else task_summarize
                if not active:
                    continue
                if isinstance(active, dict):
                    sum_lang = active.get("language")
                    sum_instructions = active.get("instructions")
                else:
                    sum_lang = None
                    sum_instructions = None
                effective_lang = sum_lang or (filter_cfg or {}).get("language") or global_language
                for item in items_per_feed.get(url, []):
                    summarize_cfg_by_id[item.id] = (effective_lang, sum_instructions)

            if summarize_cfg_by_id:
                to_summarize = [it for it in all_new_items if it.id in summarize_cfg_by_id]
                if to_summarize:
                    summarized = await _summarize_items(
                        to_summarize,
                        summarize_cfg_by_id,
                        llm_adapter,
                        evaluate_model,
                        session,
                        collector=collector,
                    )
                    by_id = {it.id: it for it in summarized}
                    all_new_items = [by_id.get(it.id, it) for it in all_new_items]

        # --- Process: LLM filter ---
        filter_result: FilterResult | None = None
        if filter_cfg and all_new_items:
            language = filter_cfg.get("language") or global_language
            filter_result = await _apply_llm_filter(
                all_new_items,
                filter_cfg,
                evaluate_model,
                language,
                memory_history,
                llm_adapter,
                collector=collector,
            )

        # Determine passing items and apply explain mode
        if filter_result is not None:
            passing = [it for it in filter_result.items if it.filter_pass is not False]
            if explain:
                passing = [
                    dc_replace(it, body=it.filter_reason or it.summary or it.body) for it in passing
                ]
            elif all(it.summary is None for it in passing):
                pass  # no summaries to apply
            else:
                passing = [dc_replace(it, body=it.summary or it.body) for it in passing]
            all_annotated = filter_result.items
            memory_text = filter_result.memory
            cite_map = filter_result.cite_map
        else:
            # No filter: apply summaries if present
            passing = [dc_replace(it, body=it.summary or it.body) for it in all_new_items]
            all_annotated = all_new_items
            memory_text = None
            cite_map = {}

        # --- Analysis mode: record push count and return without posting ---
        if collector:
            collector.record_push(len(passing))
            return {}

        # --- Push ---
        discord_format = task_discord.get("format")
        if task_type == "digest":
            target: (
                DiscordEmbedTarget | DiscordDigestTarget | DiscordMarkdownTarget | DiscordTextTarget
            ) = DiscordDigestTarget()
            ctx = PushContext(items=passing, memory=memory_text, cite_map=cite_map)
            try:
                failed_ids = await target.push(ctx, task_cfg, session)
            except Exception:
                log.error("[%s] Failed to post digest — state not saved", task_name)
                return {}
            log.info("[%s] Posted digest", task_name)
            if _get_file_path(task_cfg):
                await FileDigestTarget().push(ctx, task_cfg, session)
        elif discord_format == "markdown":
            target = DiscordMarkdownTarget()
            ctx = PushContext(items=passing, memory=memory_text, cite_map=cite_map)
            failed_ids = await target.push(ctx, task_cfg, session)
            if _get_file_path(task_cfg):
                await FileEmbedTarget().push(ctx, task_cfg, session)
        else:
            target = DiscordEmbedTarget(fetch_image=fetch_image)
            ctx = PushContext(items=passing, memory=memory_text, cite_map=cite_map)
            failed_ids = await target.push(ctx, task_cfg, session)
            if _get_file_path(task_cfg):
                await FileEmbedTarget().push(ctx, task_cfg, session)

        # --- State update ---
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
        new_feeds_state = dict(feeds_state)

        for fc in feed_cfgs:
            url = fc["url"]
            pull_result = fetch_map.get(url)
            if pull_result is None:
                continue  # failed fetch — leave existing state untouched

            prev_items = feeds_state.get(url, {}).get("items", [])
            prev_by_url = {item["url"]: item for item in prev_items}
            annotated_by_link = {it.id: it for it in all_annotated}

            if filter_cfg:
                final_items = list(prev_items)
                for ci in pull_result.current_items:
                    item_url = ci["url"]
                    if item_url not in prev_by_url:
                        state_item = dict(ci)
                        it = annotated_by_link.get(item_url)
                        if it is not None and it.summary:
                            state_item["summary"] = it.summary
                        if it is not None and it.filter_pass is not None:
                            state_item.update(
                                {
                                    "filter_pass": it.filter_pass,
                                    "filter_reason": it.filter_reason or "",
                                }
                            )
                        else:
                            state_item["filter_pass"] = True
                        final_items.append(state_item)
            else:
                new_items_state = []
                for ci in pull_result.current_items:
                    if ci["url"] not in prev_by_url:
                        state_item = dict(ci)
                        it = annotated_by_link.get(ci["url"])
                        if it is not None and it.summary:
                            state_item["summary"] = it.summary
                        new_items_state.append(state_item)
                final_items = list(prev_items) + new_items_state

            # Remove any items that failed to post
            if failed_ids:
                final_items = [item for item in final_items if item["url"] not in failed_ids]

            for item in final_items:
                if "access_date" not in item:
                    item["access_date"] = now_iso

            new_feeds_state[url] = {"items": final_items, "last_run": now_iso}

        new_task_state: dict = {"feeds": new_feeds_state}
        if filter_cfg:
            history = dict(raw_history)
            if memory_text is not None:
                _stripped = _CITE_STRIP_RE.sub("", memory_text)
                history[now_iso] = " ".join(
                    line.strip() for line in _stripped.splitlines() if line.strip()
                )
                if len(history) > 20:
                    for old_key in sorted(history)[: len(history) - 20]:
                        del history[old_key]
                log.info("[%s] Memory updated (%d chars)", task_name, len(memory_text))
            new_task_state["memory"] = history

        return {task_name: new_task_state}

    finally:
        if collector:
            collector.finish_task()


async def _process_scraper_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    global_color: int | None = None,
) -> dict:
    """Scrape a site, post new listings as Discord embeds. Returns {task_name: task_state}."""
    task_name = task_cfg["name"]
    task_discord = _get_discord_cfg(task_cfg)
    task_color = _parse_color(task_discord.get("color")) or global_color

    task_state = state.get("tasks", {}).get(task_name, {})
    prev_items = task_state.get("items", [])
    seen = {item["url"] for item in prev_items}

    source = ScraperSource()
    pull_result = await source.pull(task_cfg, seen, session)
    if pull_result is None:
        return {}

    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()

    if not pull_result.new_items:
        return {task_name: {**task_state, "last_run": now_iso}}

    colored_items = [
        dc_replace(item, meta={**item.meta, "color": task_color, "download_image": False})
        for item in pull_result.new_items
    ]

    discord_format = task_discord.get("format")
    ctx = PushContext(items=colored_items)
    if discord_format == "markdown":
        failed_ids = await DiscordMarkdownTarget().push(ctx, task_cfg, session)
    else:
        failed_ids = await DiscordEmbedTarget(fetch_image=False).push(ctx, task_cfg, session)
    if _get_file_path(task_cfg):
        await FileEmbedTarget().push(ctx, task_cfg, session)

    prev_by_url = {item["url"]: item for item in prev_items}
    merged = list(prev_items)
    for ci in pull_result.current_items:
        if ci["url"] not in prev_by_url:
            merged.append({**ci, "access_date": now_iso})
    if failed_ids:
        merged = [it for it in merged if it["url"] not in failed_ids]

    return {task_name: {"items": merged, "last_run": now_iso}}
