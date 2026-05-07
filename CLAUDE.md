# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supports two task types:
- **RSS tasks**: polls feeds, posts new entries as embeds, tracks seen IDs.
- **LLM tasks**: calls OpenAI Responses API with a prompt + `web_search_preview` tool, posts the plain-text response. Good for scheduled digests ("today's news, filter for signal > noise").

Intended to be run on a cron, not as a long-lived process.

## Commands

The project uses `uv` (see `uv.lock`, `.python-version` pinning Python 3.14).

- Run: `uv run claudinho` (reads `~/.config/claudinho/config.yaml`, writes state to `~/.local/share/claudinho/state.json`)
- Run with explicit config: `uv run claudinho config.yaml` (state defaults to `<config_dir>/state.json`)
- Run one task by name, ignoring period/last_run: `uv run claudinho --task "world-news"`
- Verbose output: add `--verbose` to any invocation
- Sync deps: `uv sync`

There is no test suite, linter, or formatter configured.

Config is read from `$XDG_CONFIG_HOME/claudinho/config.yaml` (default `~/.config/claudinho/config.yaml`) and state is written to `$XDG_DATA_HOME/claudinho/state.json` (default `~/.local/share/claudinho/state.json`). Both paths can be overridden: pass a positional config path and/or `--state`. Copy `config.yaml.template` and fill in webhook URLs and feed URLs.

## Architecture

Seven modules, no package, flat layout:

- `main.py` — CLI entry point and orchestration. Resolves config/state paths, manages a lock file, loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of three modes: normal (run due tasks in parallel), `--regenerate-state`, or `--clean`/`--migrate`.
- `config.py` — config loading and validation. `load_config(path)` reads YAML or JSON. `validate_config(config)` returns a list of error strings. Also houses `_parse_color`, `_parse_period`, and `_task_type` (infers task type from config shape: `feeds` key → RSS/digest task, `llm` key without `feeds` → LLM task).
- `state.py` — state I/O and maintenance. `load_state`, `save_state` (writes `.old` backup), and `_auto_clean` (removes malformed/expired entries, runs every 30 days).
- `tasks.py` — task execution. `_process_task` handles RSS tasks (with optional LLM filter); `_process_llm_task` handles standalone LLM tasks. `_is_due` checks period with 60s grace. **Normal mode**: filters tasks by `_is_due`, then gathers all due tasks in parallel.
- `feed.py` — feed fetching, dedup, and entry enrichment. `get_new_entries(feed_cfg, seen, session)` returns `(current_ids, new_entries)` on success or `None` on parse failure (so the caller can skip the `last_run` update and retry on the next cron tick):
  - `current_ids` is *all* IDs currently in the feed (used to overwrite state — old IDs that fall off the feed are forgotten, which keeps `state.json` from growing forever).
  - Entry ID is `entry.link`; entries with no link are skipped.
  - Unseen entries are reversed into chronological order, then OG images are fetched concurrently (only the first 32KB of each article page is read — the parser stops at `<body>`).
  - Descriptions are HTML-stripped, truncated to 300 chars, and Markdown-escaped (`_MD_ESCAPE_RE` covers `*_` `~` `` ` `` and leading `>`/`#` per line) so feed content can't accidentally format Discord messages.
- `llm.py` — two functions: `run_llm_task(task_cfg)` calls the OpenAI Responses API with `web_search_preview` and posts the plain-text response; `filter_entries(items, filter_cfg, global_model, *, context_items, memory_history)` classifies feed items and returns `(results_dict, memory_text) | None`. `results_dict` maps str(id) → `{"pass": bool, "reason": str}`; `memory_text` is the new memory log entry or `None`. `filter_entries` also supports optional `web_search` (from `filter_cfg["web_search"]`) for context/fact-checking. Requires `$OPENAI_API_KEY` in env. Default model: `gpt-5.4-mini`.
- `discord.py` — two posting functions: `post_to_discord` (embed from a `FeedEntry`) and `post_text_to_discord` (plain `content` message, truncated to 2000 chars). Both raise on HTTP ≥400.

### State shape

State is keyed by task name at the top level, matching the config structure:

```json
{
  "my-feeds": {
    "feeds": {
      "https://feed1.url": {
        "items": [{"url": "...", "title": "...", "filter_pass": true, "filter_reason": "..."}, ...],
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
```

- `feeds` sub-dict holds per-URL state. Each successful fetch replaces `items` with the feed's current entries (bounded by feed length). `filter_pass` and `filter_reason` are only present on items from tasks with an `llm` key.
- `memory` is only present on filtered RSS tasks. Each run appends one entry keyed by ISO8601 timestamp; history is capped at 20 entries (oldest evicted). The LLM receives the last 5 entries as context on each run.
- LLM tasks store only `last_run` directly under the task name key.
- `load_state` returns the parsed JSON as-is; absent or `null` `last_run` always means "due now".
- There is no migration from the old flat (URL-keyed) state format — use `--regenerate-state` to rebuild.

### Config shape

See `config.yaml.template` — it is the canonical reference for all supported keys and their defaults.

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-entry, the gather uses `return_exceptions=True` per-feed.
- Keep the 1-second sleep between posts in the same feed (Discord webhook rate limits).
- Don't add a sync HTTP path; the OG-image fetch and feed posting are deliberately concurrent via the shared session.
- Only update `last_run` on a successful feed fetch. A `None` from `get_new_entries` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
