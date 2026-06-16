# state/

State I/O and schema migrations. State shape canonical reference lives in the root `CLAUDE.md`.

## `__init__.py`

- `load_state` — parses JSON as-is; absent or `null` `last_run` always means "due now".
- `save_state` — atomic tmp-file replace, stamps `_version` and `_last_run`, sorts tasks/feeds/items for stable diffs.
- `auto_clean` — removes malformed items.
- `remove_unknown(state, config)` — prunes tasks/feeds/real-estate urls absent from config (the `--clean` mode is `auto_clean` + `remove_unknown`).

## `migrate.py` — schema migrations

`needs_migration(state)` checks `state["_version"]` against `CURRENT_VERSION` (7). `migrate(state, config=None)` steps through `_STEPS` until current; `config` is threaded to steps that need it (v5).

- **v2** — nests all task keys under a top-level `"tasks"` key.
- **v3** — renames `access_date` → `first_seen` on every item.
- **v4** — moves any task-level flat `items: [...]` (only real-estate tasks had this) into `scrapers["__legacy__"]: {items: [...]}` so dedup keeps working without re-posting old listings.
- **v5** — rekeys `scrapers[<adapter_id>]` buckets to `scrapers[<url>]` (the `adapter` config field was dropped; url is now the source identity). Uses the per-task adapter→url map from `config`; buckets with no config match fold into `__legacy__`.
- **v6** — renames each task's `scrapers` bucket to `realestate` (the "scraper" task kind was renamed to "realestate"). Pure per-task key rename; contents ride along.
- **v7** — drops the legacy prose `memory` log from each task. It was replaced by a structured `coverage.ledger`; the ledger starts fresh (repopulates within a few curate runs), so the old blob is removed, not converted.

## `state.json.template`

Example state file shape.
