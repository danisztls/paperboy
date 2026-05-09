# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supports three task types:
- **RSS tasks**: polls feeds, posts new entries as Discord embeds, tracks seen entries.
- **Digest tasks**: like RSS tasks but all passing entries are collected and posted as a single text message (splits on 2000-char limit). No OG image fetching. Uses `[Title](<url>)` to suppress Discord link previews.
- **LLM tasks**: calls OpenAI Responses API with a prompt + `web_search_preview` tool, posts the plain-text response. Good for scheduled digests ("today's news, filter for signal > noise").

Intended to be run on a cron, not as a long-lived process.

## Commands

The project uses `uv` (see `uv.lock`, `.python-version` pinning Python 3.14).

- Run: `uv run claudinho` (reads `~/.config/claudinho/config.yaml`, writes state to `~/.local/share/claudinho/state.json`)
- Run with explicit config: `uv run claudinho --config config.yaml` (state defaults to `<config_dir>/state.json`)
- Run one task by name, ignoring period/last_run: `uv run claudinho --task "world-news"`
- Verbose output: add `--verbose` to any invocation
- Validate config and exit: `uv run claudinho --validate`
- Migrate state to current schema version: `uv run claudinho --migrate`
- Sync deps: `uv sync`

There is no test suite, linter, or formatter configured.

Config is read from `$XDG_CONFIG_HOME/claudinho/config.yaml` (default `~/.config/claudinho/config.yaml`) and state is written to `$XDG_DATA_HOME/claudinho/state.json` (default `~/.local/share/claudinho/state.json`). Both paths can be overridden: pass `--config` and/or `--state`. Copy `config.yaml.template` and fill in webhook URLs and feed URLs.

Logs are written to `<state_dir>/logs/<timestamp>.log` on every run.

## Architecture

Eight modules, no package, flat layout:

- `main.py` — CLI entry point and orchestration. Resolves config/state paths, manages a lock file, loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of four modes: normal (run due tasks in parallel), `--regenerate-state`, `--clean`/`--migrate`, or `--validate`.
- `config.py` — config loading and validation. `load_config(path)` reads YAML or JSON. `validate_config(config)` uses Pydantic models to validate the full config and returns a list of error strings. Also houses `_parse_color`, `_parse_period`, and `_task_type` (returns the explicit `type:` key if present; otherwise infers: `feeds` key → RSS/digest task, `llm` key without `feeds` → LLM task).
- `state.py` — state I/O and maintenance. `load_state`, `save_state` (writes `.old` backup, stamps `_version` and `_last_run`), and `_auto_clean` (removes malformed items; only runs when `--clean` is explicitly invoked).
- `migrate.py` — state schema migrations. `needs_migration(state)` checks `state["_version"]` against `CURRENT_VERSION` (2). `migrate(state)` steps through `_STEPS` until current. The v2 migration nests all task keys under a top-level `"tasks"` key.
- `tasks.py` — task execution. `_process_task` handles RSS and digest tasks (with optional LLM filter); `_process_llm_task` handles standalone LLM tasks. `_is_due` checks period with 60s grace. `_merge_filter` combines task-level and feed-level heuristic filter dicts. `_recent_passed_items` pulls the last 7 `filter_pass=True` items across all feeds for LLM context. **Normal mode**: filters tasks by `_is_due`, then gathers all due tasks in parallel.
- `feed.py` — feed fetching, dedup, and entry enrichment. `get_new_entries(feed_cfg, seen, session)` returns `(current_items, new_entries)` on success or `None` on parse failure:
  - `current_items` is a list of `{"url": ..., "title": ...}` dicts for all entries currently in the feed (used to overwrite state — old entries that fall off the feed are forgotten).
  - Entry ID is `entry.link`; entries with no link, or older than 7 days, are skipped.
  - Unseen entries are reversed into chronological order. Descriptions are HTML-stripped, truncated to 2048 chars (Discord embed description max is 4096), and Markdown-escaped.
  - Heuristic filters (`filter.title`, `filter.description`) are applied via `_apply_regex` before enrichment. Supported ops: `extract` (regex), `replace`/`with`, `remove_phrases_with_urls`, `remove_phrases_containing`, `clear`.
- `llm.py` — two functions: `run_llm_task(task_cfg, instructions, global_model)` calls the OpenAI Responses API with `web_search_preview` and returns the plain-text response; `filter_entries(items, filter_cfg, global_model, *, language, context_items, memory_history, api_key)` classifies feed items and returns `(results_dict, memory_text) | None`. `items` is a list of source-group dicts `[{"source": ..., "items": [{"id": int, "title": ..., "description": ...}]}]`. `results_dict` maps `str(id)` → `{"pass": bool, "reason": str}`; `memory_text` is the new memory log entry or `None`. When `explain: true`, passing-item reasons are ELI5-style (2–3 sentences). `web_search` in `filter_cfg` can be `true` or a dict of options. Requires `$OPENAI_API_KEY` in env (or `api_key` in config). Default model: `gpt-5.4-mini`.
- `discord.py` — three posting functions: `post_to_discord` (embed from a `FeedEntry`, with optional OG image fetch and download/optimize as WebP attachment), `post_text_to_discord` (plain `content` message, truncated to 2000 chars), and `post_digest_to_discord` (splits memory text into ≤2000-char chunks, replaces `[n]` citation markers with `[[Source]](<url>)` Discord masked links). All use `_post_webhook` which retries once on 429. OG image fetching retries once after 2s on bot-detection (response < 2 KB).

### State shape

State is keyed by task name under a top-level `"tasks"` key. Meta keys live at the top level:

```json
{
  "_version": 2,
  "_last_run": "<iso8601 utc>",
  "tasks": {
    "my-feeds": {
      "feeds": {
        "https://feed1.url": {
          "items": [
            {"url": "...", "title": "...", "access_date": "<iso8601 utc>", "filter_pass": true, "filter_reason": "..."},
            ...
          ],
          "last_run": "<iso8601 utc>"
        }
      },
      "memory": {
        "2026-05-01T12:00:00Z": "Recurring themes this week include...",
        "2026-05-01T14:00:00Z": "Following the earlier AI announcements..."
      }
    },
    "world-news": {
      "last_run": "<iso8601 utc>"
    }
  }
}
```

- `feeds` sub-dict holds per-URL state. Each successful fetch replaces `items` with the feed's current entries (bounded by feed length). `access_date` is stamped when an item is first seen and carried forward. `filter_pass` and `filter_reason` are only present on items from tasks with an `llm` key.
- `memory` is only present on filtered RSS/digest tasks. Each run appends one entry keyed by ISO8601 timestamp; history is capped at 20 entries (oldest evicted). The LLM receives the last 7 entries as context on each run.
- LLM tasks store only `last_run` directly under the task name key.
- `load_state` returns the parsed JSON as-is; absent or `null` `last_run` always means "due now".
- Use `--migrate` to update an old state file to the current schema. Use `--regenerate-state` to rebuild state from scratch.

### Config shape

See `config.yaml.template` — it is the canonical reference for all supported keys and their defaults.

Any change that adds, removes, or renames a config key must also update the corresponding Pydantic model in `config.py` so validation stays in sync.

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-task exceptions, the gather uses `return_exceptions=True`.
- Keep the 2-second sleep between posts in the same task (Discord webhook rate limits).
- Don't add a sync HTTP path; the OG-image fetch and feed posting are deliberately concurrent via the shared session.
- Only update `last_run` on a successful feed fetch. A `None` from `get_new_entries` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
- LLM filter failures retry once after 10s before giving up on the whole task (returning `{}`).
- All tasks for a given RSS/digest task share one LLM filter call; items are sent grouped by source with monotonically increasing integer IDs across all feeds.
