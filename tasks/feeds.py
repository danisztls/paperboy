"""RSS/digest task pipeline: pull feeds → summarize → curate → push → merge state."""

import asyncio
import logging
from dataclasses import replace as dc_replace
from datetime import UTC, datetime

from config import (
    get_discord_cfg,
    get_feeds,
    get_file_path,
    is_youtube_feed_url,
    parse_color,
    parse_period,
    task_kind,
)
from config.scope import layer_dict, resolve_scoped
from pipeline import Citation, CoverageUpdate, CurateResult, Item, MemoryParagraph, PushContext
from process.curate import curate_items
from process.summarize import summarize_items
from pull.feed import RSSSource
from push.discord import (
    DiscordDigestTarget,
    DiscordEmbedTarget,
    DiscordMarkdownTarget,
)
from push.file import FileDigestTarget, FileItemTarget
from tasks.context import RunContext
from tasks.due import DEFAULT_PERIOD, due_feeds
from tasks.feed_state import build_feed_task_state
from util import utc_now_iso

log = logging.getLogger(__name__)


async def pull_feeds(
    source: RSSSource,
    feed_cfgs: list[dict],
    feeds_state: dict,
    ctx: RunContext,
    task_cfg: dict,
) -> tuple[dict[str, object], dict[str, dict]]:
    """Fetch all feeds concurrently. Returns ({url: PullResult | None}, {url: filter_log})."""

    async def _fetch_one(fc: dict):
        url = fc["url"]
        seen = (
            set()
            if ctx.analysis
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
            merged = resolve_scoped(key, ctx.config, task_cfg, fc, youtube=is_yt and yt_scoped)
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
            if ctx.collector
            else None
        )
        return await source.pull(effective_fc, seen, ctx.session, filter_log=filter_log), filter_log

    results = await asyncio.gather(*[_fetch_one(fc) for fc in feed_cfgs], return_exceptions=True)

    fetch_map: dict[str, object] = {}
    filter_log_map: dict[str, dict] = {}
    for fc, item in zip(feed_cfgs, results):
        if isinstance(item, Exception):
            log.error("Feed fetch failed for %s: %s: %s", fc["url"], type(item).__name__, item)
            continue
        pull_result, filter_log = item
        fetch_map[fc["url"]] = pull_result
        if filter_log is not None:
            filter_log_map[fc["url"]] = filter_log

    return fetch_map, filter_log_map


def _collect_tagged_items(
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    filter_log_map: dict[str, dict],
    ctx: RunContext,
    task_cfg: dict,
) -> tuple[list[Item], dict[str, list[Item]]]:
    """Tag new items from each feed with display metadata; record feed stats to collector."""
    analysis_limit = ctx.collector.limit if (ctx.analysis and ctx.collector) else 0
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

        if ctx.collector:
            fl = filter_log_map.get(url, {})
            ctx.collector.record_feed(
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
            layer_dict(ctx.config.get("discord"), get_discord_cfg(task_cfg), fc.get("discord")).get(
                "color"
            )
        )
        feed_skip_image = bool(
            resolve_scoped(
                "ignore", ctx.config, task_cfg, fc, youtube=is_youtube_feed_url(url)
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


async def _summarize_stage(
    all_new_items: list[Item],
    items_per_feed: dict[str, list[Item]],
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    ctx: RunContext,
    task_summarize,
) -> list[Item]:
    """Build per-item summarize config and run summarize_items; returns updated items."""
    cfg_by_id: dict[str, tuple[str | None, str | None]] = {}
    for fc in feed_cfgs:
        url = fc["url"]
        if fetch_map.get(url) is None:
            continue
        feed_summarize = fc.get("summarize")
        active = feed_summarize if feed_summarize is not None else task_summarize
        if not active:
            continue
        if isinstance(active, dict):
            sum_lang = active.get("language")  # None → LLM mirrors content's language
            sum_instructions = active.get("instructions")
        else:
            sum_lang = None
            sum_instructions = None
        for item in items_per_feed.get(url, []):
            cfg_by_id[item.id] = (sum_lang, sum_instructions)

    to_summarize = [it for it in all_new_items if it.id in cfg_by_id]
    if not to_summarize:
        return all_new_items
    summarized = await summarize_items(
        to_summarize,
        cfg_by_id,
        ctx.llm.summarize,
        collector=ctx.collector,
        analysis=ctx.analysis,
    )
    by_id = {it.id: it for it in summarized}
    return [by_id.get(it.id, it) for it in all_new_items]


def _select_passing(
    curate_result: CurateResult | None,
    all_new_items: list[Item],
    *,
    explain: bool,
) -> tuple[
    list[Item],
    list[Item],
    list[MemoryParagraph] | None,
    dict[int, Citation],
    list[CoverageUpdate] | None,
]:
    """Pick items to post and substitute body text. Returns
    (passing, all_annotated, memory_paragraphs, cite_map, coverage). The digest briefing
    (memory_paragraphs) is derived from this run's coverage updates."""
    if curate_result is not None:
        passing = [it for it in curate_result.items if it.filter_pass is not False]
        if explain:
            passing = [
                dc_replace(it, body=it.filter_reason or it.summary or it.body) for it in passing
            ]
        elif any(it.summary for it in passing):
            passing = [dc_replace(it, body=it.summary or it.body) for it in passing]
        memory_paragraphs = (
            [
                MemoryParagraph(text=c.update or c.state, citations=c.citations, section=c.section)
                for c in curate_result.coverage
            ]
            if curate_result.coverage
            else None
        )
        return (
            passing,
            curate_result.items,
            memory_paragraphs,
            curate_result.cite_map,
            curate_result.coverage,
        )
    passing = [dc_replace(it, body=it.summary or it.body) for it in all_new_items]
    return passing, all_new_items, None, {}, None


async def _push_stage(
    *,
    kind: str,
    task_cfg: dict,
    passing: list[Item],
    memory_paragraphs: list[MemoryParagraph] | None,
    cite_map: dict[int, Citation],
    task_name: str,
    ctx: RunContext,
) -> set[str] | None:
    """Pick target by kind + format and push. Returns failed_ids, or None if a digest post failed."""
    push_ctx = PushContext(items=passing, memory=memory_paragraphs, cite_map=cite_map)
    if kind == "digest":
        try:
            failed_ids = await DiscordDigestTarget().push(push_ctx, task_cfg, ctx.session)
        except Exception:
            log.error("[%s] Failed to post digest — state not saved", task_name)
            return None
        log.info("[%s] Posted digest to Discord", task_name)
        if get_file_path(task_cfg):
            await FileDigestTarget().push(push_ctx, task_cfg, ctx.session)
        return failed_ids

    target: DiscordEmbedTarget | DiscordMarkdownTarget
    if get_discord_cfg(task_cfg).get("format") == "markdown":
        target = DiscordMarkdownTarget()
    else:
        target = DiscordEmbedTarget()
    failed_ids = await target.push(push_ctx, task_cfg, ctx.session)
    posted = len(passing) - len(failed_ids)
    if posted > 0:
        log.info("[%s] Posted %d item(s) to Discord", task_name, posted)
    if get_file_path(task_cfg):
        await FileItemTarget().push(push_ctx, task_cfg, ctx.session)
    return failed_ids


async def process_feed_task(task_cfg: dict, state: dict, ctx: RunContext) -> dict:
    """Pull RSS feeds, optionally summarize/curate, push to Discord. Returns {task_name: task_state}."""
    task_name = task_cfg["name"]
    curate_cfg = task_cfg.get("curate") or None
    explain = bool(curate_cfg.get("explain")) if curate_cfg else False
    if ctx.analysis and curate_cfg:
        explain = True
    kind = task_kind(task_cfg)
    task_summarize = task_cfg.get("summarize", kind == "digest")

    with ctx.capture_task(task_name, kind):
        feed_cfgs = [fc for fc in get_feeds(task_cfg) if fc.get("url")]
        if ctx.analysis and ctx.collector and ctx.collector.limit_feeds > 0:
            feed_cfgs = feed_cfgs[: ctx.collector.limit_feeds]
        task_state = state.get("tasks", {}).get(task_name, {})
        feeds_state = task_state.get("feeds", {})

        # Per-feed period gating: a feed with its own `period:` is only fetched when
        # its own clock has elapsed; the rest pass through with their state untouched.
        # Bypassed for forced (--task) and analysis runs, and for digest (which posts
        # all feeds together and rejects per-feed period at validation).
        if not ctx.analysis and not ctx.force and kind != "digest":
            task_period = parse_period(task_cfg.get("period", DEFAULT_PERIOD))
            feed_cfgs = due_feeds(feed_cfgs, feeds_state, task_period, datetime.now(UTC))

        prev_coverage = task_state.get("coverage", {}) if curate_cfg else {}

        # --- Pull ---
        source = RSSSource(ctx.max_age_seconds)
        fetch_map, filter_log_map = await pull_feeds(source, feed_cfgs, feeds_state, ctx, task_cfg)

        # --- Tag with per-feed display metadata ---
        all_new_items, items_per_feed = _collect_tagged_items(
            feed_cfgs, fetch_map, filter_log_map, ctx, task_cfg
        )

        # --- Summarize ---
        if all_new_items:
            all_new_items = await _summarize_stage(
                all_new_items, items_per_feed, feed_cfgs, fetch_map, ctx, task_summarize
            )

        # --- Curate ---
        curate_result: CurateResult | None = None
        if curate_cfg and all_new_items:
            curate_result = await curate_items(
                all_new_items,
                curate_cfg,
                ctx.llm.curate,
                language=curate_cfg.get("language") or ctx.language,
                ledger=prev_coverage.get("ledger") or None,
                rollups=prev_coverage.get("rollups") or None,
                collector=ctx.collector,
                analysis=ctx.analysis,
                task_name=task_name,
            )

        passing, all_annotated, memory_paragraphs, cite_map, coverage = _select_passing(
            curate_result, all_new_items, explain=explain
        )

        ctx.record_push(len(passing))
        if ctx.analysis:
            return {}

        # --- Push ---
        failed_ids = await _push_stage(
            kind=kind,
            task_cfg=task_cfg,
            passing=passing,
            memory_paragraphs=memory_paragraphs,
            cite_map=cite_map,
            task_name=task_name,
            ctx=ctx,
        )
        if failed_ids is None:
            return {}

        # --- State update ---
        return {
            task_name: build_feed_task_state(
                feed_cfgs=feed_cfgs,
                fetch_map=fetch_map,
                feeds_state=feeds_state,
                all_annotated=all_annotated,
                has_curate=bool(curate_cfg),
                failed_ids=failed_ids,
                prev_coverage=prev_coverage,
                coverage=coverage,
                task_name=task_name,
            )
        }


async def regenerate_feeds_state(config: dict, state: dict, ctx: RunContext) -> None:
    """Fetch all feeds and write current items into state without posting (--regenerate-state)."""
    now = utc_now_iso()
    source = RSSSource(ctx.max_age_seconds)
    for task_cfg in config.get("tasks", []):
        if task_kind(task_cfg) != "feeds":
            continue
        task_name = task_cfg.get("name")
        if not task_name:
            log.warning("Skipping feeds task with no name")
            continue
        task_state = state.setdefault("tasks", {}).setdefault(task_name, {})
        feeds_state = task_state.setdefault("feeds", {})
        for feed_cfg in get_feeds(task_cfg):
            url = feed_cfg.get("url")
            if not url:
                continue
            pull_result = await source.pull(feed_cfg, set(), ctx.session)
            if pull_result is None:
                log.warning("Failed to fetch %s, skipping", url)
                continue
            prev_seen = {
                item["url"]: item["first_seen"]
                for item in feeds_state.get(url, {}).get("items", [])
                if "first_seen" in item
            }
            for item in pull_result.current_items:
                item["first_seen"] = prev_seen.get(item["url"], now)
            feed_dict: dict = {"items": pull_result.current_items, "last_run": now}
            if pull_result.name:
                feed_dict["name"] = pull_result.name
            feeds_state[url] = feed_dict
            log.info("Regenerated %d items for %s", len(pull_result.current_items), url)
