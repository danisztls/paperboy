import asyncio
import logging
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from config import (
    Period,
    get_discord_cfg,
    get_feeds,
    get_file_path,
    get_finance_cfg,
    get_weather_cfg,
    is_youtube_feed_url,
    parse_color,
    task_kind,
)
from config.scope import layer_dict
from pipeline import Citation, FilterResult, Item, MemoryParagraph, PushContext
from process._vasco import fetch_content
from process.curate import curate_entries
from process.summarize import summarize_entry
from providers.llm.base import LLMAdapter
from pull.feed import RSSSource
from pull.finance import FinanceSource
from pull.realestate import _get_realestate_cfgs, pull_realestate
from pull.search import run_search_task
from pull.weather import WeatherSource, _climate_cache_fresh, fetch_climate_normals
from push.discord import (
    DiscordDigestTarget,
    DiscordEmbedTarget,
    DiscordMarkdownTarget,
    DiscordTextTarget,
)
from push.file import FileDigestTarget, FileItemTarget

DEFAULT_PERIOD = Period(count=1, unit="h")
PERIOD_GRACE = timedelta(seconds=60)

log = logging.getLogger(__name__)


def _effective_reasoning(default: str | bool | dict | None, analysis: bool) -> bool | str | dict:
    """`--analysis` forces reasoning on; otherwise honor the per-spec default."""
    if analysis:
        return True
    return default or False


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
    post are dropped. first_seen is stamped on any item that lacks it.
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
        if "first_seen" not in item:
            item["first_seen"] = now_iso
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


def _is_due(feed_state: dict, period: Period, now: datetime) -> bool:
    last_run = feed_state.get("last_run")
    if not last_run:
        return True
    try:
        last = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    if not period.is_calendar:
        return (now - last) >= period.as_timedelta() - PERIOD_GRACE
    last_local = last.astimezone().date()
    now_local = now.astimezone().date()
    if period.unit == "d":
        return (now_local - last_local).days >= period.count
    # period.unit == "w" — ISO week, Monday-anchored
    ly, lw, _ = last_local.isocalendar()
    ny, nw, _ = now_local.isocalendar()
    return (ny * 53 + nw) - (ly * 53 + lw) >= period.count


def _resolve_scoped(key: str, global_cfg: dict, task_cfg: dict, fc: dict, *, youtube: bool) -> dict:
    """layer_dict a scoped block (global→task→feed) by `key`. When `youtube` is True (feed is a
    YouTube feed), interleave the global/task `youtube.<key>` contributions at the matching scope,
    so a global `youtube.ignore.description` is overridable per task/feed."""
    blocks: list = [global_cfg.get(key)]
    if youtube:
        blocks.append((global_cfg.get("youtube") or {}).get(key))
    blocks.append(task_cfg.get(key))
    if youtube:
        blocks.append((task_cfg.get("youtube") or {}).get(key))
    blocks.append(fc.get(key))
    return layer_dict(*blocks)


async def _pull_feeds(
    source: RSSSource,
    feed_cfgs: list[dict],
    feeds_state: dict,
    global_cfg: dict,
    task_cfg: dict,
    session: aiohttp.ClientSession,
    *,
    collector=None,
    analysis: bool = False,
) -> tuple[dict[str, object], dict[str, dict]]:
    """Fetch all feeds concurrently. Returns ({url: PullResult | None}, {url: filter_log})."""

    async def _fetch_one(fc: dict):
        url = fc["url"]
        seen = (
            set()
            if analysis
            else {item["url"] for item in feeds_state.get(url, {}).get("items", [])}
        )
        is_yt = is_youtube_feed_url(url)
        overrides: dict = {}
        for key, yt_scoped in (
            ("ignore", True),
            ("skip", True),
            ("description", False),
            ("title", False),
        ):
            merged = _resolve_scoped(key, global_cfg, task_cfg, fc, youtube=is_yt and yt_scoped)
            if merged:
                overrides[key] = merged
        effective_fc = {**fc, **overrides} if overrides else fc
        filter_log = (
            {
                "url_excluded": [],
                "shorts_excluded": [],
                "livestream_excluded": [],
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
    for fc, item in zip(feed_cfgs, results):
        if isinstance(item, Exception):
            log.error("Feed fetch failed for %s: %s: %s", fc["url"], type(item).__name__, item)
            continue
        _, pull_result, filter_log = item
        fetch_map[fc["url"]] = pull_result
        if filter_log is not None:
            filter_log_map[fc["url"]] = filter_log

    return fetch_map, filter_log_map


async def _summarize_items(
    items: list[Item],
    cfg_by_id: dict[str, tuple[str, str | None]],
    summarize_adapter: LLMAdapter | None,
    model: str | None,
    *,
    default_reasoning: str | bool | dict | None = None,
    collector=None,
    analysis: bool = False,
) -> list[Item]:
    """Replace .summary on items that have fetchable content or a body, concurrently.

    Also fills Item.image with the article's og:image when the item had no image
    yet, piggybacking on the content fetch vasco already performed.
    """

    async def _get_content(e: Item) -> tuple[str, str | None]:
        fetched = await fetch_content(e.url)
        if fetched:
            return fetched
        return e.body, None

    if summarize_adapter is None:
        log.error("Summarize skipped — summarize.model is not configured")
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
                summarize_adapter,
                model=model,
                language=cfg_by_id[e.id][0],
                instructions=cfg_by_id[e.id][1],
                reasoning=_effective_reasoning(default_reasoning, analysis),
                trace=trace,
            )
        except Exception as exc:
            log.error("summarize_entry failed for %s: %s", e.url, exc)
            summary = None
        return e, content, summary, og_image, trace

    def _dedup_key(e: Item) -> str:
        return e.url or f"__no_url__:{e.id}"

    seen_keys: set[str] = set()
    unique: list[Item] = []
    for e in items:
        key = _dedup_key(e)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(e)

    results = await asyncio.gather(*[_fetch_and_summarize(e) for e in unique])

    by_key: dict[str, tuple[str | None, str | None]] = {}
    for e, content, summary, og_image, trace in results:
        by_key[_dedup_key(e)] = (summary, og_image)
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

    updated: dict[str, Item] = {}
    for e in items:
        summary, og_image = by_key.get(_dedup_key(e), (None, None))
        fields: dict = {}
        if summary:
            fields["summary"] = summary
        if og_image and not e.image:
            fields["image"] = og_image
        if fields:
            updated[e.id] = dc_replace(e, **fields)
    return [updated.get(e.id, e) for e in items]


async def _apply_curate(
    all_items: list[Item],
    filter_cfg: dict,
    model: str | None,
    language: str,
    memory_history: list[tuple[str, str]] | None,
    curate_adapter: LLMAdapter | None,
    *,
    default_reasoning: str | bool | dict | None = None,
    collector=None,
    analysis: bool = False,
) -> FilterResult:
    """Run LLM curate filter on items. Returns FilterResult with filter_pass set on each item."""
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
            if item.meta.get("curate_skip"):
                continue
            payload_item: dict = {"id": global_id, "title": item.title, "url": item.url}
            desc = item.summary or item.body
            if desc:
                payload_item["description"] = desc
            group["items"].append(payload_item)
            id_map[global_id] = item
            global_id += 1
        if group["items"]:
            payload_groups.append(group)

    cite_map: dict[int, Citation] = {
        gid: Citation(item.source, item.url) for gid, item in id_map.items()
    }

    if not payload_groups:
        return FilterResult(items=all_items, memory=None, cite_map=cite_map)

    if curate_adapter is None:
        log.error("LLM curate skipped — curate.model is not configured")
        return FilterResult(items=all_items, memory=None, cite_map=cite_map)

    filter_trace: dict | None = {} if collector else None
    effective_filter_cfg = {**filter_cfg, "explain": True} if analysis else filter_cfg
    _filter_kwargs = dict(
        language=language,
        memory_history=memory_history,
        adapter=curate_adapter,
        extra_instructions=effective_filter_cfg.get("instructions") or None,
        reasoning=_effective_reasoning(default_reasoning, analysis),
        trace=filter_trace,
    )
    llm_return = await curate_entries(payload_groups, effective_filter_cfg, model, **_filter_kwargs)
    if llm_return is None:
        log.warning("Filter failed, retrying in 10s")
        await asyncio.sleep(10)
        llm_return = await curate_entries(
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

    raw_results, memory_paragraphs = llm_return

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
        memory_for_trace = (
            "\n\n".join(p.text for p in memory_paragraphs) if memory_paragraphs else None
        )
        collector.record_filter(
            model=model,
            instructions=filter_trace.get("instructions", ""),
            payload=filter_trace.get("payload", []),
            raw_response=filter_trace.get("raw_response"),
            parsed=parsed_list,
            memory=memory_for_trace,
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

    return FilterResult(items=annotated, memory=memory_paragraphs, cite_map=cite_map)


async def _process_search_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    instructions: str | None = None,
    search_model: str | None = None,
    search_adapter: LLMAdapter | None,
    search_reasoning: str | bool | dict | None = None,
    collector=None,
    analysis: bool = False,
) -> dict:
    """Pull from LLM web search, post as plain text. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    if collector:
        collector.begin_task(name, "search")
    try:
        if search_adapter is None:
            log.error("[%s] Skipping — search.model is not configured", name)
            return {}

        trace: dict | None = {} if collector else None
        text = await run_search_task(
            task_cfg,
            instructions,
            search_model,
            adapter=search_adapter,
            reasoning=_effective_reasoning(search_reasoning, analysis),
            trace=trace,
        )
        if collector and trace is not None:
            collector.record_search(
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
                [Item(id=f"{name}:search_result", title=name, source=name, body=text)]
                if text
                else []
            )
            if collector:
                collector.record_push(len(new_items_preview))
            return {}

        if not text:
            return {}
        new_items = [Item(id=f"{name}:search_result", title=name, source=name, body=text)]

        target = DiscordTextTarget()
        ctx = PushContext(items=new_items)
        try:
            await target.push(ctx, task_cfg, session)
        except Exception:
            log.error("Skipping search task %s due to post failure", name)
            return {}
        log.info("[%s] Posted response (%d chars)", name, len(text))

        if get_file_path(task_cfg):
            await FileItemTarget().push(ctx, task_cfg, session)

        if collector:
            collector.record_push(len(new_items))
        return {name: {"last_run": datetime.now(UTC).replace(microsecond=0).isoformat()}}
    finally:
        if collector:
            collector.finish_task()


async def _process_weather_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    collector=None,
    analysis: bool = False,
) -> dict:
    """Fetch Open-Meteo forecast, post as plain text. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    if collector:
        collector.begin_task(name, "weather")
    try:
        weather_cfg = dict(get_weather_cfg(task_cfg))

        fresh_climate: dict | None = None
        if weather_cfg.get("kind") == "smart":
            tz = ZoneInfo(weather_cfg["timezone"])
            now_local = datetime.now(tz)
            cache = state.get("tasks", {}).get(name, {}).get("climate")
            if _climate_cache_fresh(cache, now_local):
                weather_cfg["_climate_normals"] = cache
            else:
                fresh_climate = await fetch_climate_normals(weather_cfg, session)
                weather_cfg["_climate_normals"] = fresh_climate or cache

        result = await WeatherSource().pull(weather_cfg, set(), session)
        if result is None or not result.new_items:
            return {}

        if analysis:
            if collector:
                collector.record_push(len(result.new_items))
            return {}

        ctx = PushContext(items=result.new_items)
        try:
            await DiscordTextTarget().push(ctx, task_cfg, session)
        except Exception:
            log.error("Skipping weather task %s due to post failure", name)
            return {}
        log.info("[%s] Posted", name)

        if get_file_path(task_cfg):
            await FileItemTarget().push(ctx, task_cfg, session)

        if collector:
            collector.record_push(len(result.new_items))
        task_state = {"last_run": datetime.now(UTC).replace(microsecond=0).isoformat()}
        if fresh_climate is not None:
            task_state["climate"] = fresh_climate
        return {name: task_state}
    finally:
        if collector:
            collector.finish_task()


async def _process_finance_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    collector=None,
    analysis: bool = False,
) -> dict:
    """Fetch yfinance quotes, post report or batched monitor alerts. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    if collector:
        collector.begin_task(name, "finance")
    try:
        finance_cfg = dict(get_finance_cfg(task_cfg))
        finance_cfg["_task_name"] = name

        task_state = state.get("tasks", {}).get(name, {})
        is_monitor = "monitor" in finance_cfg
        if is_monitor:
            finance_cfg["_state_tickers"] = task_state.get("tickers", {})
            finance_cfg["_last_run"] = task_state.get("last_run")

        result = await FinanceSource().pull(finance_cfg, set(), session)
        if result is None:
            return {}

        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
        out_state: dict = {"last_run": now_iso}
        if is_monitor:
            out_state["tickers"] = finance_cfg.get("_new_state_tickers", {})

        if not result.new_items:
            # Monitor with zero alerts: still persist state so next tick has baselines.
            if collector:
                collector.record_push(0)
            return {name: out_state} if is_monitor else {}

        if analysis:
            if collector:
                collector.record_push(len(result.new_items))
            return {}

        ctx = PushContext(items=result.new_items)
        try:
            await DiscordTextTarget().push(ctx, task_cfg, session)
        except Exception:
            log.error("Skipping finance task %s due to post failure", name)
            return {}
        log.info("[%s] Posted", name)

        if get_file_path(task_cfg):
            await FileItemTarget().push(ctx, task_cfg, session)

        if collector:
            collector.record_push(len(result.new_items))
        return {name: out_state}
    finally:
        if collector:
            collector.finish_task()


def _collect_tagged_items(
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    filter_log_map: dict[str, dict],
    *,
    global_cfg: dict,
    task_cfg: dict,
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

        feed_color = parse_color(
            layer_dict(global_cfg.get("discord"), get_discord_cfg(task_cfg), fc.get("discord")).get(
                "color"
            )
        )
        feed_skip_image = bool(
            _resolve_scoped(
                "ignore", global_cfg, task_cfg, fc, youtube=is_youtube_feed_url(url)
            ).get("image")
        )
        feed_curate_cfg = fc.get("curate")
        feed_curate_skip = (
            bool(feed_curate_cfg.get("skip")) if feed_curate_cfg is not None else False
        )
        feed_meta = {
            "color": feed_color,
            "skip_image": feed_skip_image,
            "curate_skip": feed_curate_skip,
        }
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
    summarize_adapter: LLMAdapter | None,
    summarize_model: str | None,
    summarize_reasoning: str | bool | dict | None = None,
    collector,
    analysis: bool,
) -> list[Item]:
    """Build per-item summarize config and run _summarize_items; returns updated items."""
    summarize_cfg_by_id: dict[str, tuple[str | None, str | None]] = {}
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
        effective_lang = sum_lang  # None → LLM mirrors content's language
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
        summarize_adapter,
        summarize_model,
        default_reasoning=summarize_reasoning,
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
) -> tuple[list[Item], list[Item], list[MemoryParagraph] | None, dict[int, Citation]]:
    """Pick items to post and substitute body text. Returns (passing, all_annotated, memory, cite_map)."""
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
    memory_paragraphs: list[MemoryParagraph] | None,
    cite_map: dict[int, Citation],
    task_name: str,
    session: aiohttp.ClientSession,
) -> set[str] | None:
    """Pick target by kind + format and push. Returns failed_ids, or None if a digest post failed."""
    ctx = PushContext(items=passing, memory=memory_paragraphs, cite_map=cite_map)
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
    posted = len(passing) - len(failed_ids)
    if posted > 0:
        log.info("[%s] Posted %d item(s)", task_name, posted)
    if get_file_path(task_cfg):
        await FileItemTarget().push(ctx, task_cfg, session)
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
    memory_paragraphs: list[MemoryParagraph] | None,
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
        feed_dict: dict = {"items": final_items, "last_run": now_iso}
        if pull_result.name:
            feed_dict["name"] = pull_result.name
        new_feeds_state[url] = feed_dict

    new_task_state: dict = {"feeds": new_feeds_state}
    if has_filter:
        history = dict(raw_history)
        if memory_paragraphs is not None:
            joined = "\n\n".join(p.text for p in memory_paragraphs)
            history[now_iso] = " ".join(
                line.strip() for line in joined.splitlines() if line.strip()
            )
            if len(history) > 20:
                for old_key in sorted(history)[: len(history) - 20]:
                    del history[old_key]
            log.info("[%s] Memory updated (%d chars)", task_name, len(joined))
        new_task_state["memory"] = history
    return new_task_state


async def _process_feed_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    curate_model: str | None = None,
    curate_adapter: LLMAdapter | None = None,
    curate_reasoning: str | bool | dict | None = None,
    summarize_model: str | None = None,
    summarize_adapter: LLMAdapter | None = None,
    summarize_reasoning: str | bool | dict | None = None,
    global_cfg: dict | None = None,
    global_language: str = "EN-US",
    max_age_seconds: int | None = None,
    collector=None,
    analysis: bool = False,
) -> dict:
    """Pull RSS feeds, optionally filter/summarize, push to Discord. Returns {task_name: task_state}."""
    global_cfg = global_cfg or {}
    task_name = task_cfg["name"]
    task_discord = get_discord_cfg(task_cfg)
    filter_cfg = task_cfg.get("curate") or None
    explain = bool(filter_cfg.get("explain")) if filter_cfg else False
    if analysis and filter_cfg:
        explain = True
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
            global_cfg,
            task_cfg,
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
            global_cfg=global_cfg,
            task_cfg=task_cfg,
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
                summarize_adapter=summarize_adapter,
                summarize_model=summarize_model,
                summarize_reasoning=summarize_reasoning,
                collector=collector,
                analysis=analysis,
            )

        # --- Filter ---
        filter_result: FilterResult | None = None
        if filter_cfg and all_new_items:
            language = filter_cfg.get("language") or global_language
            filter_result = await _apply_curate(
                all_new_items,
                filter_cfg,
                curate_model,
                language,
                memory_history,
                curate_adapter,
                default_reasoning=curate_reasoning,
                collector=collector,
                analysis=analysis,
            )

        passing, all_annotated, memory_paragraphs, cite_map = _select_passing(
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
            memory_paragraphs=memory_paragraphs,
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
                memory_paragraphs=memory_paragraphs,
                task_name=task_name,
            )
        }
    finally:
        if collector:
            collector.finish_task()


async def _process_realestate_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    global_cfg: dict | None = None,
) -> dict:
    """Scrape one or more sites, post new listings as one batched Discord stream.

    State is per-url (`realestate[<url>]: {items, last_run}`). A failed source
    preserves its prior state; task-level `last_run` advances if at least one
    source succeeded. The `__legacy__` bucket (from the v3→v4 migration)
    contributes URLs to every source's `seen` set for dedup but is never
    written to.
    """
    global_cfg = global_cfg or {}
    task_name = task_cfg["name"]
    task_discord = get_discord_cfg(task_cfg)
    task_color = parse_color(layer_dict(global_cfg.get("discord"), task_discord).get("color"))

    task_state = state.get("tasks", {}).get(task_name, {})
    realestate_state = task_state.get("realestate", {})
    legacy_seen = {
        it["url"] for it in realestate_state.get("__legacy__", {}).get("items", []) if "url" in it
    }

    realestate_cfgs = _get_realestate_cfgs(task_cfg)
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

    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()

    all_new_items: list[Item] = []
    for result in results.values():
        if result is None:
            continue
        for it in result.new_items:
            all_new_items.append(dc_replace(it, meta={**it.meta, "color": task_color}))

    failed_ids: set[str] = set()
    if all_new_items:
        discord_format = task_discord.get("format")
        ctx = PushContext(items=all_new_items)
        if discord_format == "markdown":
            failed_ids = await DiscordMarkdownTarget().push(ctx, task_cfg, session)
        else:
            failed_ids = await DiscordEmbedTarget().push(ctx, task_cfg, session)
        posted = len(all_new_items) - len(failed_ids)
        if posted > 0:
            log.info("[%s] Posted %d listing(s)", task_name, posted)
        if get_file_path(task_cfg):
            await FileItemTarget().push(ctx, task_cfg, session)

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
