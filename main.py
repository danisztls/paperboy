#!/usr/bin/env python3
"""RSS to Discord webhook notifier"""

import asyncio
import json
import sys
import pathlib
import logging
import argparse
from datetime import datetime, timedelta, timezone

import aiohttp

from feed import get_new_entries
from discord import post_to_discord, post_text_to_discord
from llm import run_llm_task, filter_entries

DEFAULT_PERIOD_HOURS = 1.0
PERIOD_GRACE = timedelta(seconds=60)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def load_config(path: pathlib.Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _task_type(task_cfg: dict) -> str:
    return "llm" if "prompt" in task_cfg else "feeds"


def _recent_passed_items(task_state: dict, n: int = 7) -> list[dict]:
    """Return the n most recent pass_filter=True items across all feeds in this task's state."""
    passed = []
    for feed_state in task_state.get("feeds", {}).values():
        for item in feed_state.get("items", []):
            if item.get("pass_filter") is True:
                passed.append({"title": item.get("title", ""), "url": item.get("url", "")})
    return passed[:n]


def _is_due(feed_state: dict, period_hours: float, now: datetime) -> bool:
    last_run = feed_state.get("last_run")
    if not last_run:
        return True
    try:
        last = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    threshold = timedelta(hours=period_hours) - PERIOD_GRACE
    return (now - last) >= threshold


async def _process_llm_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    instructions: str | None = None,
    llm_model: str | None = None,
) -> dict:
    """Run one LLM task, post the response, return {name: task_state} on success or {} on failure."""
    name = task_cfg["name"]
    text = await run_llm_task(task_cfg, instructions, llm_model)
    if text is None:
        return {}
    webhook = task_cfg["webhook"]
    try:
        await post_text_to_discord(webhook, text, session)
        log.info("[%s] Posted LLM response (%d chars)", name, len(text))
    except Exception:
        log.error("Skipping LLM task %s due to post failure", name)
        return {}
    return {name: {"last_run": datetime.now(timezone.utc).isoformat()}}


async def _process_task(
    task_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
    *,
    llm_model: str | None = None,
) -> dict:
    """Run one RSS task (filtered or not), return {task_name: task_state}."""
    task_name = task_cfg["name"]
    webhook = task_cfg["webhook"]
    filter_cfg = task_cfg.get("filter") or None
    feed_cfgs = [fc for fc in task_cfg.get("feeds", []) if fc.get("url")]
    task_state = state.get(task_name, {})
    feeds_state = task_state.get("feeds", {})

    # Memory history (filtered tasks only)
    raw_history = task_state.get("memory", {}) if filter_cfg else {}
    memory_history: list[str] | None = None
    if raw_history:
        keys = sorted(raw_history)[-5:]
        memory_history = [raw_history[k] for k in keys] or None

    # Fetch all feeds concurrently
    fetch_og = task_cfg.get("og_images", True)

    async def _fetch_one(fc: dict):
        url = fc["url"]
        seen = {item["url"] for item in feeds_state.get(url, {}).get("items", [])}
        return fc, await get_new_entries(fc, seen, session, fetch_og_images=fetch_og)

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
                group["items"].append({"id": global_id, "title": entry.title})
                id_map[global_id] = entry
                global_id += 1
            payload_groups.append(group)

    # One LLM filter call for the whole task
    filter_result: dict | None = None
    memory_text: str | None = None
    if filter_cfg and payload_groups:
        llm_return = await filter_entries(
            payload_groups, filter_cfg, llm_model,
            context_items=_recent_passed_items(task_state),
            memory_history=memory_history,
        )
        if llm_return is None:
            log.warning("[%s] Filter failed, posting all entries", task_name)
        else:
            raw_results, memory_text = llm_return
            filter_result = {
                id_map[int(gid)].id: v
                for gid, v in raw_results.items()
                if gid.isdigit() and int(gid) in id_map
            }

    now_iso = datetime.now(timezone.utc).isoformat()
    new_feeds_state = dict(feeds_state)  # carry forward state for feeds not fetched this run

    for fc in feed_cfgs:
        url = fc["url"]
        fetch = feed_fetch_map.get(url)
        if fetch is None:
            continue  # failed fetch — leave existing state untouched
        current_items, new_entries = fetch

        if filter_cfg:
            if filter_result is None:
                passing_ids = {e.id for e in new_entries}
            else:
                passing_ids = {eid for eid, v in filter_result.items() if v["pass"]}
            entries_to_post = [e for e in new_entries if e.id in passing_ids]
        else:
            entries_to_post = new_entries

        # Build annotated state items
        if filter_cfg:
            new_entry_by_link = {e.link: e for e in new_entries}
            prev_by_url = {item["url"]: item for item in feeds_state.get(url, {}).get("items", [])}
            final_items = []
            for item in current_items:
                item_url = item["url"]
                if item_url in new_entry_by_link:
                    e = new_entry_by_link[item_url]
                    if filter_result is not None and e.id in filter_result:
                        v = filter_result[e.id]
                        final_items.append({**item, "pass_filter": v["pass"], "filter_reason": v["reason"]})
                    else:
                        final_items.append({**item, "pass_filter": True})
                elif "pass_filter" in prev_by_url.get(item_url, {}):
                    prev = prev_by_url[item_url]
                    extra = {"pass_filter": prev["pass_filter"]}
                    if "filter_reason" in prev:
                        extra["filter_reason"] = prev["filter_reason"]
                    final_items.append({**item, **extra})
                else:
                    final_items.append(item)
        else:
            final_items = current_items

        for i, entry in enumerate(entries_to_post):
            try:
                await post_to_discord(webhook, entry, session)
                log.info("[%s] Posted: %s", entry.feed_title, entry.title[:80])
                if i < len(entries_to_post) - 1:
                    await asyncio.sleep(1)
            except Exception:
                log.error("Skipping entry %s due to post failure", entry.id)

        new_feeds_state[url] = {"items": final_items, "last_run": now_iso}

    new_task_state: dict = {"feeds": new_feeds_state}
    if filter_cfg:
        history = dict(raw_history)
        if memory_text is not None:
            history[now_iso] = memory_text
            if len(history) > 20:
                for old_key in sorted(history)[:len(history) - 20]:
                    del history[old_key]
            log.info("[%s] Memory updated (%d chars)", task_name, len(memory_text))
        new_task_state["memory"] = history

    return {task_name: new_task_state}


async def _async_main(args: argparse.Namespace) -> None:
    config_path = pathlib.Path(args.config).expanduser().resolve()
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else config_path.parent / "state.json"
    )

    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    log.info("Config: %s", config_path)
    log.info("State:  %s", state_path)

    config = load_config(config_path)
    state = load_state(state_path)
    llm_cfg = config.get("llm", {})
    instructions = llm_cfg.get("instructions") or None
    llm_model = llm_cfg.get("model") or None

    tasks = config.get("tasks", [])
    if not tasks:
        log.error("No tasks defined in config.")
        sys.exit(1)

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
    ) as session:
        if args.regenerate_state:
            now = datetime.now(timezone.utc).isoformat()
            for task_cfg in tasks:
                if _task_type(task_cfg) != "feeds":
                    continue
                task_name = task_cfg.get("name")
                if not task_name:
                    log.warning("Skipping feeds task with no name")
                    continue
                task_state = state.setdefault(task_name, {})
                feeds_state = task_state.setdefault("feeds", {})
                fetch_og = task_cfg.get("og_images", True)
                for feed_cfg in task_cfg.get("feeds", []):
                    url = feed_cfg.get("url")
                    if not url:
                        continue
                    result = await get_new_entries(feed_cfg, set(), session, fetch_og_images=fetch_og)
                    if result is None:
                        log.warning("Failed to fetch %s, skipping", url)
                        continue
                    current_items, _ = result
                    feeds_state[url] = {"items": current_items, "last_run": now}
                    log.info("Regenerated %d items for %s", len(current_items), url)
            save_state(state_path, state)
            log.info("Done. State regenerated and saved to %s", state_path)
        elif args.debug:
            # Sequential: run one task, stop after the first successful post.
            # State is never saved in debug mode.
            for task_cfg in tasks:
                webhook = task_cfg.get("webhook")
                if not webhook:
                    log.warning("Skipping task with no webhook URL")
                    continue
                if _task_type(task_cfg) == "llm":
                    name = task_cfg.get("name")
                    if not name:
                        log.warning("Skipping LLM task with no name")
                        continue
                    text = await run_llm_task(task_cfg, instructions, llm_model)
                    if text:
                        await post_text_to_discord(webhook, text, session, debug=True)
                        log.info("[%s] Posted LLM response (%d chars)", name, len(text))
                        log.debug("Debug mode: stopping after first LLM task")
                        return
                else:
                    task_filter_cfg = task_cfg.get("filter") or None
                    task_name = task_cfg.get("name", "")
                    task_state = state.get(task_name, {})
                    feeds_state = task_state.get("feeds", {})

                    # Fetch all feeds and build global-ID payload
                    fetch_og = task_cfg.get("og_images", True)
                    global_id = 0
                    id_map: dict[int, object] = {}
                    feed_entries: list[tuple[dict, list]] = []
                    payload_groups: list[dict] = []
                    for feed_cfg in task_cfg.get("feeds", []):
                        url = feed_cfg.get("url")
                        if not url:
                            log.warning("Skipping feed with no URL: %s", feed_cfg)
                            continue
                        seen = {item["url"] for item in feeds_state.get(url, {}).get("items", [])}
                        result = await get_new_entries(feed_cfg, seen, session, fetch_og_images=fetch_og)
                        if result is None:
                            continue
                        _current_items, new_entries = result
                        feed_entries.append((feed_cfg, new_entries))
                        if new_entries and task_filter_cfg:
                            group = {"source": new_entries[0].feed_title, "items": []}
                            for entry in new_entries:
                                group["items"].append({"id": global_id, "title": entry.title})
                                id_map[global_id] = entry
                                global_id += 1
                            payload_groups.append(group)

                    # One filter call for the whole task (memory discarded in debug mode)
                    passing_entry_ids: set[str] | None = None
                    if task_filter_cfg and payload_groups:
                        raw_history = task_state.get("memory", {})
                        memory_history = [raw_history[k] for k in sorted(raw_history)[-5:]] or None
                        llm_return = await filter_entries(
                            payload_groups, task_filter_cfg, llm_model,
                            context_items=_recent_passed_items(task_state),
                            memory_history=memory_history,
                        )
                        if llm_return is not None:
                            raw_results, _ = llm_return
                            passing_entry_ids = {
                                id_map[int(gid)].id
                                for gid, v in raw_results.items()
                                if v["pass"] and gid.isdigit() and int(gid) in id_map
                            }
                        else:
                            passing_entry_ids = {e.id for _, entries in feed_entries for e in entries}

                    for _feed_cfg, new_entries in feed_entries:
                        visible = (
                            new_entries if passing_entry_ids is None
                            else [e for e in new_entries if e.id in passing_entry_ids]
                        )
                        if visible:
                            await post_to_discord(webhook, visible[0], session, debug=True)
                            log.info("[%s] Posted: %s", visible[0].feed_title, visible[0].title[:80])
                            log.debug("Debug mode: stopping after first posted entry")
                            return
            log.debug("Debug mode: no new entries found in any feed")
        else:
            # Concurrent: all tasks run in parallel.
            now = datetime.now(timezone.utc)
            feed_tasks = []
            force_task = args.task

            task_list = tasks
            if force_task:
                task_list = [t for t in tasks if t.get("name") == force_task]
                if not task_list:
                    log.error("No task named %r found in config", force_task)
                    sys.exit(1)

            for task_cfg in task_list:
                webhook = task_cfg.get("webhook")
                if not webhook:
                    continue
                period = float(task_cfg.get("period", DEFAULT_PERIOD_HOURS))
                if _task_type(task_cfg) == "llm":
                    name = task_cfg.get("name")
                    if not name:
                        log.warning("Skipping LLM task with no name")
                        continue
                    task_state = state.get(name, {"last_run": None})
                    if not force_task and not _is_due(task_state, period, now):
                        last = datetime.fromisoformat(task_state["last_run"])
                        mins = int((now - last).total_seconds() // 60)
                        log.info(
                            "[%s] Skipping — last run %d min ago, period is %g h",
                            name, mins, period,
                        )
                        continue
                    feed_tasks.append(_process_llm_task(task_cfg, state, session, instructions=instructions, llm_model=llm_model))
                else:
                    task_name = task_cfg.get("name")
                    if not task_name:
                        log.warning("Skipping feeds task with no name")
                        continue
                    feeds_state = state.get(task_name, {}).get("feeds", {})
                    feed_urls = [f["url"] for f in task_cfg.get("feeds", []) if f.get("url")]
                    if not force_task and not any(
                        _is_due(feeds_state.get(u, {"last_run": None}), period, now) for u in feed_urls
                    ):
                        log.info("[%s] Skipping — no feeds are due", task_name)
                        continue
                    feed_tasks.append(_process_task(task_cfg, state, session, llm_model=llm_model))

            results = await asyncio.gather(*feed_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.error("Feed task failed: %s", result)
                elif result:
                    state.update(result)
            save_state(state_path, state)
            log.info("Done. State saved to %s", state_path)


def main():
    parser = argparse.ArgumentParser(description="RSS to Discord webhook notifier")
    parser.add_argument("config", help="Path to config file (YAML or JSON)")
    parser.add_argument(
        "--state",
        default=None,
        help="Path to state file (default: <config_dir>/state.json)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--debug",
        action="store_true",
        help="Post one entry from the first feed with new content; skip state save",
    )
    mode.add_argument(
        "--task",
        metavar="NAME",
        help="Run a single task by name, ignoring period and last_run state",
    )
    mode.add_argument(
        "--regenerate-state",
        action="store_true",
        help="Fetch all feeds and write current items to state without posting to Discord",
    )
    args = parser.parse_args()

    if args.verbose or args.debug:
        for name in ("__main__", "feed", "llm", "discord"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    if args.debug:
        log.debug("Debug mode enabled — will parse one feed, post one entry, skip state save")

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
