"""State file schema migrations."""

CURRENT_VERSION = 4


def needs_migration(state: dict) -> bool:
    return state.get("_version", 0) < CURRENT_VERSION


def _to_v2(state: dict) -> dict:
    """Nest all task keys under 'tasks:' (v0/v1 → v2)."""
    _meta = {"_version", "_last_run", "_last_clean"}
    tasks = {k: v for k, v in state.items() if k not in _meta}
    new = {k: v for k, v in state.items() if k in _meta}
    new["tasks"] = tasks
    new["_version"] = 2
    return new


def _to_v3(state: dict) -> dict:
    """Rename access_date → first_seen on every item (v2 → v3)."""
    for task_state in state.get("tasks", {}).values():
        if not isinstance(task_state, dict):
            continue
        for feed_state in task_state.get("feeds", {}).values():
            for item in feed_state.get("items", []):
                if "access_date" in item:
                    item["first_seen"] = item.pop("access_date")
        for item in task_state.get("items", []):
            if "access_date" in item:
                item["first_seen"] = item.pop("access_date")
    state["_version"] = 3
    return state


def _to_v4(state: dict) -> dict:
    """Move flat scraper `items` into `scrapers["__legacy__"]` bucket (v3 → v4).

    Only scraper tasks have a task-level `items` key (feeds nest under `feeds`;
    search/weather/finance have no items). The `__legacy__` bucket keeps the URLs
    around for dedup so we don't re-post old listings; it has no adapter writer
    so its contents only shrink as URLs cycle out of new pulls' current_items.
    """
    for task_state in state.get("tasks", {}).values():
        if not isinstance(task_state, dict):
            continue
        items = task_state.get("items")
        if isinstance(items, list):
            task_state.pop("items")
            scrapers = task_state.setdefault("scrapers", {})
            scrapers.setdefault("__legacy__", {"items": items})
    state["_version"] = 4
    return state


_STEPS = {0: _to_v2, 1: _to_v2, 2: _to_v3, 3: _to_v4}


def migrate(state: dict) -> dict:
    version = state.get("_version", 0)
    while version < CURRENT_VERSION:
        fn = _STEPS.get(version)
        if fn is None:
            raise ValueError(f"No migration defined from version {version}")
        state = fn(state)
        version = state.get("_version", 0)
    return state
