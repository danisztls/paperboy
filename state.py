import json
import logging
import pathlib
from datetime import datetime, timezone

from migrate import CURRENT_VERSION

log = logging.getLogger(__name__)


def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["_last_run"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    state["_version"] = CURRENT_VERSION
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
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

                ad = item.get("access_date")
                if not ad:
                    log.warning("%s: removing item %s: missing access_date", label, url[:80])
                    removed += 1
                    continue
                try:
                    datetime.fromisoformat(ad)
                except ValueError:
                    log.warning("%s: removing item %s: invalid access_date %r", label, url[:80], ad)
                    removed += 1
                    continue

                kept.append(item)
            feed_state["items"] = kept

    log.info("Clean: removed %d malformed entries", removed)


def _remove_unknown(state: dict, known_tasks: set, known_feeds: dict) -> None:
    """Remove state for tasks/feeds no longer present in config."""
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
