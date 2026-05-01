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
from llm import run_llm_task

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


def _migrate_state(state: dict) -> dict:
    for val in state.values():
        if isinstance(val, dict) and "ids" in val and "items" not in val:
            val["items"] = [{"url": eid, "title": ""} for eid in val.pop("ids")]
    return state


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _task_type(task_cfg: dict) -> str:
    return "llm" if "prompt" in task_cfg else "feeds"


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
    """Run one LLM task, post the response, return {state_key: task_state} on success or {} on failure."""
    name = task_cfg["name"]
    state_key = name
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
    return {state_key: {"last_run": datetime.now(timezone.utc).isoformat()}}


async def _process_feed(
    webhook: str,
    feed_cfg: dict,
    state: dict,
    session: aiohttp.ClientSession,
) -> dict:
    """Fetch one feed, post new entries, return {url: feed_state} for state update.

    Returns {} if the feed fetch failed, so the caller leaves the existing
    last_run untouched and the feed will be retried on the next run.
    """
    url = feed_cfg["url"]
    seen = {item["url"] for item in state.get(url, {}).get("items", [])}
    result = await get_new_entries(feed_cfg, seen, session)
    if result is None:
        return {}
    current_items, new_entries = result
    for i, entry in enumerate(new_entries):
        try:
            await post_to_discord(webhook, entry, session)
            log.info("[%s] Posted: %s", entry.feed_title, entry.title[:80])
            if i < len(new_entries) - 1:
                await asyncio.sleep(1)
        except Exception:
            log.error("Skipping entry %s due to post failure", entry.id)
    return {url: {"items": current_items, "last_run": datetime.now(timezone.utc).isoformat()}}


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
    state = _migrate_state(load_state(state_path))
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
        headers={"User-Agent": "rss-discord/1.0"},
    ) as session:
        if args.regenerate_state:
            now = datetime.now(timezone.utc).isoformat()
            for task_cfg in tasks:
                if _task_type(task_cfg) != "feeds":
                    continue
                for feed_cfg in task_cfg.get("feeds", []):
                    url = feed_cfg.get("url")
                    if not url:
                        continue
                    result = await get_new_entries(feed_cfg, set(), session)
                    if result is None:
                        log.warning("Failed to fetch %s, skipping", url)
                        continue
                    current_items, _ = result
                    state[url] = {"items": current_items, "last_run": now}
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
                    for feed_cfg in task_cfg.get("feeds", []):
                        url = feed_cfg.get("url")
                        if not url:
                            log.warning("Skipping feed with no URL: %s", feed_cfg)
                            continue
                        seen = {item["url"] for item in state.get(url, {}).get("items", [])}
                        result = await get_new_entries(feed_cfg, seen, session)
                        if result is None:
                            continue
                        _current_items, new_entries = result
                        if new_entries:
                            await post_to_discord(webhook, new_entries[0], session, debug=True)
                            log.info("[%s] Posted: %s", new_entries[0].feed_title, new_entries[0].title[:80])
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
                    for feed_cfg in task_cfg.get("feeds", []):
                        url = feed_cfg.get("url")
                        if not url:
                            continue
                        feed_state = state.get(url, {"ids": [], "last_run": None})
                        if not force_task and not _is_due(feed_state, period, now):
                            last = datetime.fromisoformat(feed_state["last_run"])
                            mins = int((now - last).total_seconds() // 60)
                            log.info(
                                "[%s] Skipping — last run %d min ago, period is %g h",
                                feed_cfg.get("name") or url, mins, period,
                            )
                            continue
                        feed_tasks.append(_process_feed(webhook, feed_cfg, state, session))
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
        logging.getLogger().setLevel(logging.DEBUG)
    if args.debug:
        log.debug("Debug mode enabled — will parse one feed, post one entry, skip state save")

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
