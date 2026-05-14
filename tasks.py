import asyncio
import logging
import re
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta

import aiohttp

from config import get_discord_cfg, get_feeds, get_file_path, parse_color, task_kind
from pipeline import FilterResult, Item, PushContext
from process.filter_llm import filter_entries
from process.summarize import fetch_item_content, summarize_entry
from providers.llm.base import LLMAdapter
from pull.feed import RSSSource
from pull.llm import run_llm_task
from pull.scraper import ScraperSource
from push.discord import (
    DiscordDigestTarget,
    DiscordEmbedTarget,
    DiscordMarkdownTarget,
    DiscordTextTarget,
)
from push.file import FileDigestTarget, FileEmbedTarget

DEFAULT_PERIOD = timedelta(hours=1)
PERIOD_GRACE = timedelta(seconds=60)
_CITE_STRIP_RE = re.compile(r"\s*\[\d+\]")

log = logging.getLogger(__name__)


def _merge_feed_state(
    prev_items: list[dict],
    current_items: list[dict],
    annotated_by_link: dict[str, Item],
    *,
    has_filter: bool,
    failed_ids: set[str],
    now_iso: str,
) -> list[dict]:
    """Merge prior feed state with new pull results.

    Unseen current items become state dicts with optional summary and (under
    a filter) filter_pass/filter_reason annotations. Items that failed to
    post are dropped. access_date is stamped on any item that lacks it.
    """
    prev_by_url = {item["url"]: item for item in prev_items}
    new_items: list[dict] = []
    for ci in current_items:
        if ci["url"] in prev_by_url:
            continue
        state_item = dict(ci)
        it = annotated_by_link.get(ci["url"])
        if it is not None and it.summary:
            state_item["summary"] = it.summary
        if has_filter:
            if it is not None and it.filter_pass is not None:
                state_item["filter_pass"] = it.filter_pass
                state_item["filter_reason"] = it.filter_reason or ""
            else:
                state_item["filter_pass"] = True
        new_items.append(state_item)

    final = list(prev_items) + new_items
    if failed_ids:
        final = [item for item in final if item["url"] not in failed_ids]
    for item in final:
        if "access_date" not in item:
            item["access_date"] = now_iso
    return final


def _decode_filter_results(
    raw_results: dict[str, dict], id_map: dict[int, Item]
) -> dict[str, dict]:
    """Map the LLM's int-id results to {item.id: {pass, reason}} using the int→Item map."""
    decoded: dict[str, dict] = {}
    for gid, v in raw_results.items():
        if gid.isdigit() and (item := id_map.get(int(gid))):
            decoded[item.id] = v
    return decoded


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
    analysis: bool = False,
) -> tuple[dict[str, object], dict[str, dict]]:
    """Fetch all feeds concurrently. Returns ({url: PullResult | None}, {url: filter_log})."""
    task_filter = task_filter or {}

    async def _fetch_one(fc: dict):
        url = fc["url"]
        seen = (
            set()
            if analysis
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
    analysis: bool = False,
) -> list[Item]:
    """Replace .summary on items that have fetchable content or a body, concurrently.

    Also fills Item.image with the article's og:image when the item had no image
    yet, piggybacking on the HTML fetch trafilatura already performed.
    """

    async def _get_content(e: Item) -> tuple[str, str | None]:
        """Return (content, og_image). og_image is None for body-only fallback or YouTube."""
        if session:
            fetched = await fetch_item_content(e.url, session)
            if fetched:
                return fetched
        return e.body, None

    if llm_adapter is None:
        log.error("Summarize skipped — llm.models.reasoning is not configured")
        return items

    async def _fetch_and_summarize(
        e: Item,
    ) -> tuple[Item, str | None, str | None, str | None, dict | None]:
        content, og_image = await _get_content(e)
        if not content:
            return e, None, None, None, None
        trace: dict | None = {} if collector else None
        try:
            summary = await summarize_entry(
                e.title,
                content,
                llm_adapter,
                model=model,
                language=cfg_by_id[e.id][0],
                instructions=cfg_by_id[e.id][1],
                reasoning=analysis,
                trace=trace,
            )
        except Exception as exc:
            log.error("summarize_entry failed for %s: %s", e.url, exc)
            summary = None
        return e, content, summary, og_image, trace

    results = await asyncio.gather(*[_fetch_and_summarize(e) for e in items])

    updated: dict[str, Item] = {}
    for e, content, summary, og_image, trace in results:
        fields: dict = {}
        if summary:
            fields["summary"] = summary
        if og_image and not e.image:
            fields["image"] = og_image
        if fields:
            updated[e.id] = dc_replace(e, **fields)
        if collector and trace is not None:
            collector.record_summarization(
                item_id=e.id,
                title=e.title,
                url=e.url,
                fetched_body=content,
                instructions=trace.get("instructions", ""),
                input_text=trace.get("input", ""),
                summary=summary,
                model_used=trace.get("model_used"),
                input_tokens=trace.get("input_tokens"),
                output_tokens=trace.get("output_tokens"),
                latency_s=trace.get("latency_s"),
                reasoning=trace.get("reasoning"),
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
    analysis: bool = False,
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
            payload_item: dict = {"id": global_id, "title": item.title, "url": item.url}
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

    filter_trace: dict | None = {} if collector else None
    effective_filter_cfg = {**filter_cfg, "explain": True} if analysis else filter_cfg
    _filter_kwargs = dict(
        language=language,
        memory_history=memory_history,
        adapter=llm_adapter,
        extra_instructions=effective_filter_cfg.get("instructions") or None,
        reasoning=analysis,
        trace=filter_trace,
    )
    llm_return = await filter_entries(payload_groups, effective_filter_cfg, model, **_filter_kwargs)
    if llm_return is None:
        log.warning("Filter failed, retrying in 10s")
        await asyncio.sleep(10)
        llm_return = await filter_entries(
            payload_groups, effective_filter_cfg, model, **_filter_kwargs
        )
    if llm_return is None:
        if collector and filter_trace is not None:
            collector.record_filter(
                model=model,
                instructions=filter_trace.get("instructions", ""),
                payload=filter_trace.get("payload", []),
                raw_response=filter_trace.get("raw_response"),
                parsed=[],
                memory=None,
                model_used=filter_trace.get("model_used"),
                input_tokens=filter_trace.get("input_tokens"),
                output_tokens=filter_trace.get("output_tokens"),
                latency_s=filter_trace.get("latency_s"),
                reasoning=filter_trace.get("reasoning"),
                web_search=filter_trace.get("web_search", False),
            )
        log.error("Filter failed twice — treating all items as passing")
        return FilterResult(items=all_items, memory=None, cite_map=cite_map)

    raw_results, memory_text = llm_return

    if collector and filter_trace is not None:
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
                    "url": it.url if it else None,
                    "pass": v["pass"],
                    "reason": v["reason"],
                }
            )
        collector.record_filter(
            model=model,
            instructions=filter_trace.get("instructions", ""),
            payload=filter_trace.get("payload", []),
            raw_response=filter_trace.get("raw_response"),
            parsed=parsed_list,
            memory=memory_text,
            model_used=filter_trace.get("model_used"),
            input_tokens=filter_trace.get("input_tokens"),
            output_tokens=filter_trace.get("output_tokens"),
            latency_s=filter_trace.get("latency_s"),
            reasoning=filter_trace.get("reasoning"),
            web_search=filter_trace.get("web_search", False),
        )

    result_by_item_id = _decode_filter_results(raw_results, id_map)

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
    analysis: bool = False,
) -> dict:
    """Pull from LLM web search, post as plain text. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    if collector:
        collector.begin_task(name, "llm_search")
    try:
        if llm_adapter is None:
            log.error("[%s] Skipping — llm.models.topic is not configured", name)
            return {}

        trace: dict | None = {} if collector else None
        text = await run_llm_task(
            task_cfg,
            instructions,
            search_model,
            adapter=llm_adapter,
            reasoning=analysis,
            trace=trace,
        )
        if collector and trace is not None:
            collector.record_llm_search(
                model=trace.get("model"),
                instructions=trace.get("instructions"),
                prompt=trace.get("prompt", ""),
                raw_response=text,
                model_used=trace.get("model_used"),
                input_tokens=trace.get("input_tokens"),
                output_tokens=trace.get("output_tokens"),
                latency_s=trace.get("latency_s"),
                reasoning=trace.get("reasoning"),
                web_search=trace.get("web_search", True),
            )

        if analysis:
            new_items_preview = (
                [Item(id=f"{name}:llm_result", title=name, source=name, body=text)] if text else []
            )
            if collector:
                collector.record_push(len(new_items_preview))
            return {}

        if not text:
            return {}
        new_items = [Item(id=f"{name}:llm_result", title=name, source=name, body=text)]

        target = DiscordTextTarget()
        ctx = PushContext(items=new_items)
        try:
            await target.push(ctx, task_cfg, session)
        except Exception:
            log.error("Skipping LLM task %s due to post failure", name)
            return {}

        if get_file_path(task_cfg):
            await FileEmbedTarget().push(ctx, task_cfg, session)

        if collector:
            collector.record_push(len(new_items))
        return {name: {"last_run": datetime.now(UTC).replace(microsecond=0).isoformat()}}
    finally:
        if collector:
            collector.finish_task()


def _collect_tagged_items(
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    filter_log_map: dict[str, dict],
    *,
    task_color: int | None,
    global_color: int | None,
    task_skip_image: bool,
    collector,
    analysis_limit: int,
) -> tuple[list[Item], dict[str, list[Item]]]:
    """Tag new items from each feed with display metadata; record feed stats to collector."""
    items_per_feed: dict[str, list[Item]] = {}
    all_new_items: list[Item] = []
    for fc in feed_cfgs:
        url = fc["url"]
        pull_result = fetch_map.get(url)
        if pull_result is None:
            items_per_feed[url] = []
            continue
        feed_items = pull_result.new_items
        if analysis_limit > 0:
            feed_items = feed_items[-analysis_limit:]
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

        feed_color = parse_color(fc.get("discord", {}).get("color")) or task_color or global_color
        feed_image_cfg = fc.get("image")
        feed_skip_image = (
            bool(feed_image_cfg.get("skip")) if feed_image_cfg is not None else task_skip_image
        )
        feed_meta = {"color": feed_color, "skip_image": feed_skip_image}
        all_new_items.extend(
            [dc_replace(item, meta={**item.meta, **feed_meta}) for item in feed_items]
        )
    return all_new_items, items_per_feed


async def _run_summarize_stage(
    all_new_items: list[Item],
    items_per_feed: dict[str, list[Item]],
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    *,
    task_summarize,
    filter_cfg: dict | None,
    global_language: str,
    llm_adapter: LLMAdapter | None,
    evaluate_model: str | None,
    session: aiohttp.ClientSession,
    collector,
    analysis: bool,
) -> list[Item]:
    """Build per-item summarize config and run _summarize_items; returns updated items."""
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

    if not summarize_cfg_by_id:
        return all_new_items
    to_summarize = [it for it in all_new_items if it.id in summarize_cfg_by_id]
    if not to_summarize:
        return all_new_items
    summarized = await _summarize_items(
        to_summarize,
        summarize_cfg_by_id,
        llm_adapter,
        evaluate_model,
        session,
        collector=collector,
        analysis=analysis,
    )
    by_id = {it.id: it for it in summarized}
    return [by_id.get(it.id, it) for it in all_new_items]


def _select_passing(
    filter_result: FilterResult | None,
    all_new_items: list[Item],
    *,
    explain: bool,
) -> tuple[list[Item], list[Item], str | None, dict[int, tuple[str, str | None]]]:
    """Pick items to post and substitute body text. Returns (passing, all_annotated, memory_text, cite_map)."""
    if filter_result is not None:
        passing = [it for it in filter_result.items if it.filter_pass is not False]
        if explain:
            passing = [
                dc_replace(it, body=it.filter_reason or it.summary or it.body) for it in passing
            ]
        elif any(it.summary for it in passing):
            passing = [dc_replace(it, body=it.summary or it.body) for it in passing]
        return passing, filter_result.items, filter_result.memory, filter_result.cite_map
    passing = [dc_replace(it, body=it.summary or it.body) for it in all_new_items]
    return passing, all_new_items, None, {}


async def _push_curate(
    *,
    kind: str,
    discord_format: str | None,
    task_cfg: dict,
    passing: list[Item],
    memory_text: str | None,
    cite_map: dict,
    task_name: str,
    session: aiohttp.ClientSession,
) -> set[str] | None:
    """Pick target by kind + format and push. Returns failed_ids, or None if a digest post failed."""
    ctx = PushContext(items=passing, memory=memory_text, cite_map=cite_map)
    if kind == "digest":
        try:
            failed_ids = await DiscordDigestTarget().push(ctx, task_cfg, session)
        except Exception:
            log.error("[%s] Failed to post digest — state not saved", task_name)
            return None
        log.info("[%s] Posted digest", task_name)
        if get_file_path(task_cfg):
            await FileDigestTarget().push(ctx, task_cfg, session)
        return failed_ids

    target: DiscordEmbedTarget | DiscordMarkdownTarget
    if discord_format == "markdown":
        target = DiscordMarkdownTarget()
    else:
        target = DiscordEmbedTarget()
    failed_ids = await target.push(ctx, task_cfg, session)
    if get_file_path(task_cfg):
        await FileEmbedTarget().push(ctx, task_cfg, session)
    return failed_ids


def _build_new_task_state(
    *,
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    feeds_state: dict,
    all_annotated: list[Item],
    has_filter: bool,
    failed_ids: set[str],
    raw_history: dict,
    memory_text: str | None,
    task_name: str,
) -> dict:
    """Merge per-feed state, update memory log; returns the task_state dict."""
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    new_feeds_state = dict(feeds_state)
    annotated_by_link = {it.id: it for it in all_annotated}

    for fc in feed_cfgs:
        url = fc["url"]
        pull_result = fetch_map.get(url)
        if pull_result is None:
            continue  # failed fetch — leave existing state untouched
        final_items = _merge_feed_state(
            prev_items=feeds_state.get(url, {}).get("items", []),
            current_items=pull_result.current_items,
            annotated_by_link=annotated_by_link,
            has_filter=has_filter,
            failed_ids=failed_ids,
            now_iso=now_iso,
        )
        new_feeds_state[url] = {"items": final_items, "last_run": now_iso}

    new_task_state: dict = {"feeds": new_feeds_state}
    if has_filter:
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
    return new_task_state


async def _process_llm_curate_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    evaluate_model: str | None = None,
    llm_adapter: LLMAdapter | None,
    global_color: int | None = None,
    global_language: str = "EN-US",
    max_age_seconds: int | None = None,
    collector=None,
    analysis: bool = False,
) -> dict:
    """Pull RSS feeds, optionally filter/summarize, push to Discord. Returns {task_name: task_state}."""
    task_name = task_cfg["name"]
    task_discord = get_discord_cfg(task_cfg)
    task_color = parse_color(task_discord.get("color"))
    filter_cfg = task_cfg.get("llm") or None
    explain = bool(filter_cfg.get("explain")) if filter_cfg else False
    if analysis and filter_cfg:
        explain = True
    task_skip_image = bool((task_cfg.get("image") or {}).get("skip"))
    task_filter = task_cfg.get("filter", {})
    kind = task_kind(task_cfg)
    task_summarize = task_cfg.get("summarize", kind == "digest")

    if collector:
        collector.begin_task(task_name, kind)

    try:
        feed_cfgs = [fc for fc in get_feeds(task_cfg) if fc.get("url")]
        if analysis and collector and collector.limit_feeds > 0:
            feed_cfgs = feed_cfgs[: collector.limit_feeds]
        task_state = state.get("tasks", {}).get(task_name, {})
        feeds_state = task_state.get("feeds", {})

        raw_history = task_state.get("memory", {}) if filter_cfg else {}
        memory_history: list[tuple[str, str]] | None = None
        if raw_history:
            keys = sorted(raw_history)[-5:]
            memory_history = [(k, raw_history[k]) for k in keys] or None

        # --- Pull ---
        source = RSSSource(max_age_seconds) if max_age_seconds is not None else RSSSource()
        fetch_map, filter_log_map = await _pull_feeds(
            source,
            feed_cfgs,
            feeds_state,
            task_filter,
            session,
            collector=collector,
            analysis=analysis,
        )

        # --- Tag with per-feed display metadata ---
        analysis_limit = collector.limit if (analysis and collector) else 0
        all_new_items, items_per_feed = _collect_tagged_items(
            feed_cfgs,
            fetch_map,
            filter_log_map,
            task_color=task_color,
            global_color=global_color,
            task_skip_image=task_skip_image,
            collector=collector,
            analysis_limit=analysis_limit,
        )

        # --- Summarize ---
        if all_new_items:
            all_new_items = await _run_summarize_stage(
                all_new_items,
                items_per_feed,
                feed_cfgs,
                fetch_map,
                task_summarize=task_summarize,
                filter_cfg=filter_cfg,
                global_language=global_language,
                llm_adapter=llm_adapter,
                evaluate_model=evaluate_model,
                session=session,
                collector=collector,
                analysis=analysis,
            )

        # --- Filter ---
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
                analysis=analysis,
            )

        passing, all_annotated, memory_text, cite_map = _select_passing(
            filter_result, all_new_items, explain=explain
        )

        if collector:
            collector.record_push(len(passing))
        if analysis:
            return {}

        # --- Push ---
        failed_ids = await _push_curate(
            kind=kind,
            discord_format=task_discord.get("format"),
            task_cfg=task_cfg,
            passing=passing,
            memory_text=memory_text,
            cite_map=cite_map,
            task_name=task_name,
            session=session,
        )
        if failed_ids is None:
            return {}

        # --- State update ---
        return {
            task_name: _build_new_task_state(
                feed_cfgs=feed_cfgs,
                fetch_map=fetch_map,
                feeds_state=feeds_state,
                all_annotated=all_annotated,
                has_filter=bool(filter_cfg),
                failed_ids=failed_ids,
                raw_history=raw_history,
                memory_text=memory_text,
                task_name=task_name,
            )
        }
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
    task_discord = get_discord_cfg(task_cfg)
    task_color = parse_color(task_discord.get("color")) or global_color

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
        dc_replace(item, meta={**item.meta, "color": task_color, "skip_image": True})
        for item in pull_result.new_items
    ]

    discord_format = task_discord.get("format")
    ctx = PushContext(items=colored_items)
    if discord_format == "markdown":
        failed_ids = await DiscordMarkdownTarget().push(ctx, task_cfg, session)
    else:
        failed_ids = await DiscordEmbedTarget().push(ctx, task_cfg, session)
    if get_file_path(task_cfg):
        await FileEmbedTarget().push(ctx, task_cfg, session)

    prev_by_url = {item["url"]: item for item in prev_items}
    merged = list(prev_items)
    for ci in pull_result.current_items:
        if ci["url"] not in prev_by_url:
            merged.append({**ci, "access_date": now_iso})
    if failed_ids:
        merged = [it for it in merged if it["url"] not in failed_ids]

    return {task_name: {"items": merged, "last_run": now_iso}}
