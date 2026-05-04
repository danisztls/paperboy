#!/usr/bin/env python3
"""RSS to Discord webhook notifier"""

import asyncio
import atexit
import json
import os
import re
import sys
import pathlib
import logging
import argparse
from datetime import datetime, timedelta, timezone

import aiohttp

from feed import get_new_entries
from discord import post_to_discord, post_text_to_discord, post_digest_to_discord
from llm import run_llm_task, filter_entries

DEFAULT_PERIOD = timedelta(hours=1)
_CITE_STRIP_RE = re.compile(r'\s*\[\d+\]')
PERIOD_GRACE = timedelta(seconds=60)

_PERIOD_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _parse_color(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.startswith("#") and len(s) == 7:
        return int(s[1:], 16)
    return None


def _xdg_config_path() -> pathlib.Path:
    xdg_config = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_config / "claudinho" / "config.yaml"


def _xdg_state_path() -> pathlib.Path:
    xdg_data = pathlib.Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return xdg_data / "claudinho" / "state.json"


def _parse_period(value) -> timedelta:
    if isinstance(value, str):
        value = value.strip()
        suffix = value[-1].lower() if value else ""
        if suffix in _PERIOD_UNITS:
            return timedelta(**{_PERIOD_UNITS[suffix]: float(value[:-1])})
        return timedelta(hours=float(value))
    return timedelta(hours=float(value))

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
    explicit = task_cfg.get("type")
    if explicit:
        return explicit
    return "llm" if "prompt" in task_cfg else "feeds"


def _recent_passed_items(task_state: dict, n: int = 7) -> list[dict]:
    """Return the n most recent filter_pass=True items across all feeds in this task's state."""
    passed = []
    for feed_state in task_state.get("feeds", {}).values():
        for item in feed_state.get("items", []):
            if item.get("filter_pass") is True:
                passed.append({"title": item.get("title", ""), "url": item.get("url", "")})
    return passed[:n]


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
) -> dict:
    """Run one LLM task, post the response, return {name: task_state} on success or {} on failure."""
    name = task_cfg["name"]
    text = await run_llm_task(task_cfg, instructions, llm_model)
    if text is None:
        return {}
    webhook = task_cfg.get("discord", {}).get("webhook")
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
    global_color: int | None = None,
    global_language: str = "EN-US",
) -> dict:
    """Run one RSS task (filtered or not), return {task_name: task_state}."""
    task_name = task_cfg["name"]
    task_discord = task_cfg.get("discord", {})
    webhook = task_discord.get("webhook")
    task_color = _parse_color(task_discord.get("color"))
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
        return fc, await get_new_entries(fc, seen, session)

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
    cite_map: dict[int, str] = {gid: entry.link for gid, entry in id_map.items() if entry.link}
    if filter_cfg and payload_groups:
        language = filter_cfg.get("language") or global_language
        llm_return = await filter_entries(
            payload_groups, filter_cfg, llm_model,
            language=language,
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

    task_type = _task_type(task_cfg)
    all_entries_to_post: list = []
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
                        final_items.append({**item, "filter_pass": v["pass"], "filter_reason": v["reason"]})
                    else:
                        final_items.append({**item, "filter_pass": True})
                elif "filter_pass" in prev_by_url.get(item_url, {}):
                    prev = prev_by_url[item_url]
                    extra = {"filter_pass": prev["filter_pass"]}
                    if "filter_reason" in prev:
                        extra["filter_reason"] = prev["filter_reason"]
                    final_items.append({**item, **extra})
                else:
                    final_items.append(item)
        else:
            final_items = current_items

        if task_type != "digest":
            feed_color = _parse_color(fc.get("discord", {}).get("color")) or task_color or global_color
            all_entries_to_post.extend((feed_color, e) for e in entries_to_post)

        new_feeds_state[url] = {"items": final_items, "last_run": now_iso}

    if task_type != "digest" and all_entries_to_post:
        _far_future = datetime.max.replace(tzinfo=timezone.utc)
        all_entries_to_post.sort(key=lambda c_e: c_e[1].published or _far_future)
        for i, (entry_color, entry) in enumerate(all_entries_to_post):
            try:
                await post_to_discord(webhook, entry, session, fetch_og=fetch_og, color=entry_color)
                log.info("[%s] Posted: %s", entry.feed_title, entry.title[:80])
                if i < len(all_entries_to_post) - 1:
                    await asyncio.sleep(2)
            except Exception:
                log.error("Skipping entry %s due to post failure", entry.id)

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


async def _async_main(args: argparse.Namespace) -> None:
    xdg_defaults = args.config is None
    config_path = (
        _xdg_config_path() if xdg_defaults
        else pathlib.Path(args.config).expanduser().resolve()
    )
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else (_xdg_state_path() if xdg_defaults else config_path.parent / "state.json")
    )

    lock_path = state_path.with_suffix(".lock")
    if lock_path.exists():
        raw = lock_path.read_text().strip()
        try:
            os.kill(int(raw), 0)
        except (ValueError, ProcessLookupError):
            log.warning("Removing stale lock file (PID %s)", raw)
        else:
            log.error("Another instance is running (PID %s), exiting.", raw)
            sys.exit(1)
    lock_path.write_text(str(os.getpid()))
    atexit.register(lock_path.unlink, missing_ok=True)

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
    global_language = llm_cfg.get("language") or "EN-US"
    discord_cfg = config.get("discord", {})
    global_color = _parse_color(discord_cfg.get("color"))

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
                for feed_cfg in task_cfg.get("feeds", []):
                    url = feed_cfg.get("url")
                    if not url:
                        continue
                    result = await get_new_entries(feed_cfg, set(), session)
                    if result is None:
                        log.warning("Failed to fetch %s, skipping", url)
                        continue
                    current_items, _ = result
                    feeds_state[url] = {"items": current_items, "last_run": now}
                    log.info("Regenerated %d items for %s", len(current_items), url)
            save_state(state_path, state)
            log.info("Done. State regenerated and saved to %s", state_path)
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
                webhook = task_cfg.get("discord", {}).get("webhook")
                if not webhook:
                    continue
                period = _parse_period(task_cfg.get("period", DEFAULT_PERIOD))
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
                            "[%s] Skipping — last run %d min ago, period is %s",
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
                    feed_tasks.append(_process_task(task_cfg, state, session, llm_model=llm_model, global_color=global_color, global_language=global_language))

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
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to config file (YAML or JSON). Default: $XDG_CONFIG_HOME/claudinho/config.yaml",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="Path to state file. Default: $XDG_DATA_HOME/claudinho/state.json (or <config_dir>/state.json when config is given explicitly)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    mode = parser.add_mutually_exclusive_group()
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

    if args.verbose:
        for name in ("__main__", "feed", "llm", "discord"):
            logging.getLogger(name).setLevel(logging.DEBUG)

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
