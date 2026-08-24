# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""State file schema migrations."""

CURRENT_VERSION = 7


def needs_migration(state: dict) -> bool:
    return state.get("_version", 0) < CURRENT_VERSION


def _to_v2(state: dict, config: dict | None = None) -> dict:
    """Nest all task keys under 'tasks:' (v0/v1 → v2)."""
    _meta = {"_version", "_last_run", "_last_clean"}
    tasks = {k: v for k, v in state.items() if k not in _meta}
    new = {k: v for k, v in state.items() if k in _meta}
    new["tasks"] = tasks
    new["_version"] = 2
    return new


def _to_v3(state: dict, config: dict | None = None) -> dict:
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


def _to_v4(state: dict, config: dict | None = None) -> dict:
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


def _scraper_adapter_urls(config: dict | None) -> dict[str, dict[str, str]]:
    """Per-task ``{adapter_id: url}`` from config scraper pull items.

    Reads the (now-legacy) ``adapter`` key, tolerated as an ``extra`` field, so
    a one-shot ``--migrate`` before dropping it from config can rekey buckets to
    their url. Empty when config lacks it — callers fall back to ``__legacy__``.
    """
    mapping: dict[str, dict[str, str]] = {}
    for task in (config or {}).get("tasks", []) or []:
        name = task.get("name")
        if not name:
            continue
        per_task: dict[str, str] = {}
        for item in task.get("pull", []) or []:
            sc = (item.get("realestate") or item.get("scraper")) if isinstance(item, dict) else None
            if not sc:
                continue
            adapter, url = sc.get("adapter"), sc.get("url")
            if adapter and url:
                per_task[adapter] = url
        if per_task:
            mapping[name] = per_task
    return mapping


def _to_v5(state: dict, config: dict | None = None) -> dict:
    """Rekey scraper buckets from adapter id to url (v4 → v5).

    The ``adapter`` field is gone; scraper state is now keyed by ``url`` (like
    feeds). Buckets are renamed via the per-task adapter→url map from config.
    Any bucket without a config match (renamed/removed source, or no config)
    folds its items into ``__legacy__`` so previously-seen urls stay in the
    dedup set and old listings aren't re-posted.
    """
    adapter_urls = _scraper_adapter_urls(config)
    for task_name, task_state in state.get("tasks", {}).items():
        if not isinstance(task_state, dict):
            continue
        scrapers = task_state.get("scrapers")
        if not isinstance(scrapers, dict):
            continue
        per_task = adapter_urls.get(task_name, {})
        new_scrapers: dict[str, dict] = {}
        legacy_items = list(scrapers.get("__legacy__", {}).get("items", []))
        for key, bucket in scrapers.items():
            if key == "__legacy__":
                continue
            url = per_task.get(key)
            if url:
                new_scrapers[url] = bucket
            else:
                legacy_items.extend(bucket.get("items", []))
        if legacy_items or "__legacy__" in scrapers:
            new_scrapers["__legacy__"] = {"items": legacy_items}
        task_state["scrapers"] = new_scrapers
    state["_version"] = 5
    return state


def _to_v6(state: dict, config: dict | None = None) -> dict:
    """Rename each task's ``scrapers`` state bucket to ``realestate`` (v5 → v6).

    The "scraper" task kind was renamed to "realestate" (config key, task kind,
    and state bucket all follow vasco's realestate adapter). This is a pure
    per-task key rename; bucket contents (incl. ``__legacy__``) ride along.
    """
    for task_state in state.get("tasks", {}).values():
        if not isinstance(task_state, dict):
            continue
        if "scrapers" in task_state and "realestate" not in task_state:
            task_state["realestate"] = task_state.pop("scrapers")
    state["_version"] = 6
    return state


def _to_v7(state: dict, config: dict | None = None) -> dict:
    """Drop the legacy prose ``memory`` log from each task (v6 → v7).

    The per-run prose memory was replaced by a structured ``coverage.ledger``. The
    ledger starts fresh — it repopulates within a few curate runs — so the old
    ``memory`` blob is removed rather than converted.
    """
    for task_state in state.get("tasks", {}).values():
        if isinstance(task_state, dict):
            task_state.pop("memory", None)
    state["_version"] = 7
    return state


_STEPS = {0: _to_v2, 1: _to_v2, 2: _to_v3, 3: _to_v4, 4: _to_v5, 5: _to_v6, 6: _to_v7}


def migrate(state: dict, config: dict | None = None) -> dict:
    version = state.get("_version", 0)
    while version < CURRENT_VERSION:
        fn = _STEPS.get(version)
        if fn is None:
            raise ValueError(f"No migration defined from version {version}")
        state = fn(state, config)
        version = state.get("_version", 0)
    return state
