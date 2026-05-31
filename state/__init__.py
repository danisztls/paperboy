import json
import logging
import pathlib
from datetime import UTC, datetime

from state.migrate import CURRENT_VERSION

log = logging.getLogger(__name__)


def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _sort_state(state: dict) -> dict:
    tasks = state.get("tasks", {})
    sorted_tasks = {}
    for task_name in sorted(tasks):
        task = tasks[task_name]
        feeds = task.get("feeds")
        if feeds:
            sorted_feeds = {}
            for url in sorted(feeds):
                feed = feeds[url]
                items = feed.get("items")
                if items:
                    feed = {**feed, "items": sorted(items, key=lambda i: i.get("first_seen", ""))}
                sorted_feeds[url] = feed
            task = {**task, "feeds": sorted_feeds}
        sorted_tasks[task_name] = task
    return {**state, "tasks": sorted_tasks}


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = {
        **state,
        "_last_run": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "_version": CURRENT_VERSION,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_sort_state(stamped), indent=2, ensure_ascii=False))
    tmp.replace(path)  # atomic on POSIX


def _auto_clean(state: dict) -> None:
    """Remove malformed entries from state. Run manually via --clean."""
    removed = 0
    for task_name, task_state in state.get("tasks", {}).items():
        if not isinstance(task_state, dict):
            continue
        for feed_url, feed_state in task_state.get("feeds", {}).items():
            label = f"[{task_name}] {feed_url[:60]}"
            kept = []
            for item in feed_state.get("items", []):
                url = item.get("url", "")

                if not url:
                    log.warning("%s: removing item with no url", label)
                    removed += 1
                    continue

                if "title" not in item:
                    log.warning("%s: removing item %s: missing title", label, url[:80])
                    removed += 1
                    continue

                fs = item.get("first_seen")
                if not fs:
                    log.warning("%s: removing item %s: missing first_seen", label, url[:80])
                    removed += 1
                    continue
                try:
                    datetime.fromisoformat(fs)
                except ValueError:
                    log.warning("%s: removing item %s: invalid first_seen %r", label, url[:80], fs)
                    removed += 1
                    continue

                kept.append(item)
            feed_state["items"] = kept

    log.info("Clean: removed %d malformed entries", removed)


def _remove_unknown(
    state: dict,
    known_tasks: set,
    known_feeds: dict,
    known_realestate_urls: dict | None = None,
) -> None:
    """Remove state for tasks/feeds/real-estate sources no longer present in config.

    `known_realestate_urls` maps task_name → set of real-estate urls; the `__legacy__`
    bucket is always preserved (it has no config representation by design).
    """
    tasks_state = state.get("tasks", {})

    stale_tasks = [name for name in list(tasks_state) if name not in known_tasks]
    for name in stale_tasks:
        log.warning("Clean: removing state for unknown task %r", name)
        del tasks_state[name]

    for task_name, feed_urls in known_feeds.items():
        feeds_state = tasks_state.get(task_name, {}).get("feeds", {})
        stale_feeds = [url for url in list(feeds_state) if url not in feed_urls]
        for url in stale_feeds:
            log.warning("Clean: removing state for unknown feed %s in task %r", url[:80], task_name)
            del feeds_state[url]

    for task_name, urls in (known_realestate_urls or {}).items():
        realestate_state = tasks_state.get(task_name, {}).get("realestate", {})
        stale = [u for u in list(realestate_state) if u != "__legacy__" and u not in urls]
        for url in stale:
            log.warning(
                "Clean: removing state for unknown real-estate url %s in task %r",
                url[:80],
                task_name,
            )
            del realestate_state[url]
