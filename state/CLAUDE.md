# state/

State I/O and schema migrations. State shape canonical reference lives in the root `CLAUDE.md`.

## `__init__.py`

- `load_state` — parses JSON as-is; absent or `null` `last_run` always means "due now".
- `save_state` — writes `.old` backup, stamps `_version` and `_last_run`.
- `_auto_clean` — removes malformed items.
- `_remove_unknown` — prunes tasks/feeds absent from config.

## `migrate.py` — schema migrations

`needs_migration(state)` checks `state["_version"]` against `CURRENT_VERSION` (4). `migrate(state)` steps through `_STEPS` until current.

- **v2** — nests all task keys under a top-level `"tasks"` key.
- **v3** — renames `access_date` → `first_seen` on every item.
- **v4** — moves any task-level flat `items: [...]` (only scraper tasks had this) into `scrapers["__legacy__"]: {items: [...]}` so per-adapter dedup keeps working without re-posting old listings.

## `state.json.template`

Example state file shape.
