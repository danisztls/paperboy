import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone

from migrate import CURRENT_VERSION

log = logging.getLogger(__name__)


def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.parent / (path.name + ".old"))
    state["_last_run"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    state["_version"] = CURRENT_VERSION
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _auto_clean(state: dict) -> None:
    """Remove malformed or expired entries from state. Runs automatically when due."""
    now = datetime.now(timezone.utc)
    last_clean = state.get("_last_clean")
    if last_clean:
        try:
            if (now - datetime.fromisoformat(last_clean)).days < 30:
                return
        except ValueError:
            pass

    cutoff = now - timedelta(days=30)
    removed = expired = 0
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
                    dt = datetime.fromisoformat(ad)
                except ValueError:
                    log.warning("%s: removing item %s: invalid access_date %r", label, url[:80], ad)
                    removed += 1
                    continue
                if dt < cutoff:
                    expired += 1
                    continue

                kept.append(item)
            feed_state["items"] = kept

    state["_last_clean"] = now.replace(microsecond=0).isoformat()
    log.info("Auto-clean: removed %d malformed + %d expired entries", removed, expired)
