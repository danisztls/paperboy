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
from discord import post_to_discord

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
    raw = json.loads(path.read_text())
    # Migrate legacy shape {url: [ids]} -> {url: {"ids": [...], "last_run": None}}
    for url, value in list(raw.items()):
        if isinstance(value, list):
            raw[url] = {"ids": value, "last_run": None}
    return raw


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


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
    seen = set(state.get(url, {}).get("ids", []))
    result = await get_new_entries(feed_cfg, seen, session)
    if result is None:
        return {}
    current_ids, new_entries = result
    for i, entry in enumerate(new_entries):
        try:
            await post_to_discord(webhook, entry, session)
            log.info("[%s] Posted: %s", entry.feed_title, entry.title[:80])
            if i < len(new_entries) - 1:
                await asyncio.sleep(1)
        except Exception:
            log.error("Skipping entry %s due to post failure", entry.id)
    return {url: {"ids": current_ids, "last_run": datetime.now(timezone.utc).isoformat()}}


async def _async_main(args: argparse.Namespace) -> None:
    config_path = pathlib.Path(args.config).expanduser().resolve()
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else config_path.parent / "state.json"
    )

    if args.migrate:
        state = load_state(state_path)
        save_state(state_path, state)
        log.info("Migrated state.json schema, wrote %s", state_path)
        return

    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    log.info("Config: %s", config_path)
    log.info("State:  %s", state_path)

    config = load_config(config_path)
    state = load_state(state_path)

    hooks = config.get("hooks", [])
    if not hooks:
        log.error("No hooks defined in config.")
        sys.exit(1)

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "rss-discord/1.0"},
    ) as session:
        if args.debug:
            # Sequential: fetch one feed at a time, stop after the first post.
            # State is never saved in debug mode.
            for hook_cfg in hooks:
                webhook = hook_cfg.get("webhook")
                if not webhook:
                    log.warning("Skipping hook with no webhook URL")
                    continue
                for feed_cfg in hook_cfg.get("feeds", []):
                    url = feed_cfg.get("url")
                    if not url:
                        log.warning("Skipping feed with no URL: %s", feed_cfg)
                        continue
                    seen = set(state.get(url, {}).get("ids", []))
                    result = await get_new_entries(feed_cfg, seen, session)
                    if result is None:
                        continue
                    _current_ids, new_entries = result
                    if new_entries:
                        await post_to_discord(webhook, new_entries[0], session, debug=True)
                        log.info("[%s] Posted: %s", new_entries[0].feed_title, new_entries[0].title[:80])
                        log.debug("Debug mode: stopping after first posted entry")
                        return
            log.debug("Debug mode: no new entries found in any feed")
        else:
            # Concurrent: all feeds fetched and posted in parallel.
            now = datetime.now(timezone.utc)
            tasks = []
            for hook_cfg in hooks:
                webhook = hook_cfg.get("webhook")
                if not webhook:
                    continue
                period = float(hook_cfg.get("period", DEFAULT_PERIOD_HOURS))
                for feed_cfg in hook_cfg.get("feeds", []):
                    url = feed_cfg.get("url")
                    if not url:
                        continue
                    feed_state = state.get(url, {"ids": [], "last_run": None})
                    if not _is_due(feed_state, period, now):
                        last = datetime.fromisoformat(feed_state["last_run"])
                        mins = int((now - last).total_seconds() // 60)
                        log.info(
                            "[%s] Skipping — last run %d min ago, period is %g h",
                            feed_cfg.get("name") or url, mins, period,
                        )
                        continue
                    tasks.append(_process_feed(webhook, feed_cfg, state, session))
            results = await asyncio.gather(*tasks, return_exceptions=True)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--debug",
        action="store_true",
        help="Post one entry from the first feed with new content; skip state save",
    )
    mode.add_argument(
        "--migrate",
        action="store_true",
        help="Migrate state.json to the current schema (no feeds fetched, no posts sent)",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("Debug mode enabled — will parse one feed, post one entry, skip state save")

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
