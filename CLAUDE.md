# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supports two task types:
- **RSS tasks**: polls feeds, posts new entries as embeds, tracks seen IDs.
- **LLM tasks**: calls OpenAI Responses API with a prompt + `web_search_preview` tool, posts the plain-text response. Good for scheduled digests ("today's news, filter for signal > noise").

Intended to be run on a cron, not as a long-lived process.

## Commands

The project uses `uv` (see `uv.lock`, `.python-version` pinning Python 3.14).

- Run: `uv run claudinho config.yaml`
- Run one task by name, ignoring period/last_run: `uv run claudinho config.yaml --task "world-news"`
- Verbose output: add `--verbose` to any invocation
- Sync deps: `uv sync`

There is no test suite, linter, or formatter configured.

`config.yaml` is gitignored — copy `config.yaml.template` and fill in webhook URLs and feed URLs. `state.json` is also gitignored and is created next to the config on first run.

## Architecture

Four modules, no package, flat layout:

- `main.py` — CLI entry point and orchestration. Loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of three modes:
  - **Normal mode**: filters tasks by `_is_due` (period with 60s grace), then gathers all due tasks in parallel. Task type is inferred from config shape: `feeds` key → RSS task, `prompt` key → LLM task.
  - `feed.py` — feed fetching, dedup, and entry enrichment. `get_new_entries(feed_cfg, seen, session)` returns `(current_ids, new_entries)` on success or `None` on parse failure (so the caller can skip the `last_run` update and retry on the next cron tick):
  - `current_ids` is *all* IDs currently in the feed (used to overwrite state — old IDs that fall off the feed are forgotten, which keeps `state.json` from growing forever).
  - Entry ID resolution falls back: `entry.id` → `entry.link` → `entry.title`.
  - Unseen entries are reversed into chronological order, then OG images are fetched concurrently (only the first 32KB of each article page is read — the parser stops at `<body>`).
  - Descriptions are HTML-stripped, truncated to 300 chars, and Markdown-escaped (`_MD_ESCAPE_RE` covers `*_` `~` `` ` `` and leading `>`/`#` per line) so feed content can't accidentally format Discord messages.
- `llm.py` — two functions: `run_llm_task(task_cfg)` calls the OpenAI Responses API with the `web_search_preview` built-in tool and posts the plain-text response; `filter_entries(items, filter_cfg, global_model, *, context_items, memory_history)` is a pure classification call (no web search) that returns `(results_dict, memory_text) | None`. `results_dict` maps str(id) → `{"pass": bool, "reason": str}`; `memory_text` is the new memory log entry or `None`. Requires `$OPENAI_API_KEY` in env. Default model: `gpt-5.4-mini`.
- `discord.py` — two posting functions: `post_to_discord` (embed from a `FeedEntry`) and `post_text_to_discord` (plain `content` message, truncated to 2000 chars). Both raise on HTTP ≥400.

### State shape

State is keyed by task name at the top level, matching the config structure:

```json
{
  "my-feeds": {
    "feeds": {
      "https://feed1.url": {
        "items": [{"url": "...", "title": "...", "pass_filter": true, "filter_reason": "..."}, ...],
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

- `feeds` sub-dict holds per-URL state. Each successful fetch replaces `items` with the feed's current entries (bounded by feed length). `pass_filter` and `filter_reason` are only present on items from tasks with a `filter` key.
- `memory` is only present on filtered RSS tasks. Each run appends one entry keyed by ISO8601 timestamp; history is capped at 20 entries (oldest evicted). The LLM receives the last 5 entries as context on each run.
- LLM tasks store only `last_run` directly under the task name key.
- `load_state` returns the parsed JSON as-is; absent or `null` `last_run` always means "due now".
- There is no migration from the old flat (URL-keyed) state format — use `--regenerate-state` to rebuild.

### Config shape

```yaml
discord:                             # optional — global defaults for Discord posting
  color: "#5865F2"                   # default embed color for RSS embeds

tasks:
  # RSS task — detected by presence of 'feeds' key
  - name: my-feeds                   # required for all tasks
    discord:
      webhook: "https://discord.com/api/webhooks/.../..."
      color: "#5865F2"               # optional — overrides global discord.color for this task
    period: "30m"                    # optional, number (hours) or string with m/h/d suffix. Default: 1h
    filter:                          # optional — LLM pre-post filter for all feeds in this task
      prompt: "Only keep items about AI and machine learning"
      model: gpt-4o-mini             # optional, falls back to global llm.model then default
    og_images: false                 # optional — skip OG image fetching (default: true)
    feeds:
      - name: "Display name"         # optional, used as embed footer
        url: "https://example.com/feed.xml"
        discord:
          color: "#FF0000"           # optional — overrides task discord.color for this feed

  # LLM task — detected by presence of 'prompt' key
  - name: world-news
    discord:
      webhook: "https://discord.com/api/webhooks/.../..."
    period: "24h"
    prompt: "Today news. World. Filter for signal > noise."
    model: gpt-5.4-mini              # optional, default: gpt-5.4-mini
    tools:                           # optional, merged into web_search_preview config
      allowed_domains:
        - reuters.com
      user_location:
        type: approximate
        country: US
```

Embed color resolution order: feed `discord.color` → task `discord.color` → global `discord.color` → hardcoded default (`#5865F2`). Values are CSS-style hex strings (e.g. `"#FF0000"`). Color only applies to RSS embed tasks; LLM and digest tasks post plain text. Multiple `tasks` entries let different groups go to different webhooks. `period` is per-task only; accepts a plain number (hours, for backward compat) or a string with suffix `m` (minutes), `h` (hours), or `d` (days) — e.g. `"30m"`, `"6h"`, `"1d"`. The threshold check subtracts `PERIOD_GRACE` (60s) so a 1h cron firing every ~60min doesn't skip every other tick due to clock jitter. Both YAML and JSON are accepted (dispatched by file suffix in `load_config`).

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-entry, the gather uses `return_exceptions=True` per-feed.
- Keep the 1-second sleep between posts in the same feed (Discord webhook rate limits).
- Don't add a sync HTTP path; the OG-image fetch and feed posting are deliberately concurrent via the shared session.
- Only update `last_run` on a successful feed fetch. A `None` from `get_new_entries` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
