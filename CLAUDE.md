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
- Debug a single entry without touching state: `uv run claudinho config.yaml --debug`
- Run one task by name, ignoring period/last_run: `uv run claudinho config.yaml --task "world-news"`
- Sync deps: `uv sync`

There is no test suite, linter, or formatter configured.

`config.yaml` is gitignored — copy `config.yaml.template` and fill in webhook URLs and feed URLs. `state.json` is also gitignored and is created next to the config on first non-debug run.

## Architecture

Four modules, no package, flat layout:

- `main.py` — CLI entry point and orchestration. Loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of three modes:
  - **Normal mode**: filters tasks by `_is_due` (period with 60s grace), then gathers all due tasks in parallel. Task type is inferred from config shape: `feeds` key → RSS task, `prompt` key → LLM task.
  - **Debug mode (`--debug`)**: runs the first task sequentially — for LLM tasks posts the full response; for RSS tasks posts the first new entry. Period is ignored. **State is never saved in debug mode.**
  - `feed.py` — feed fetching, dedup, and entry enrichment. `get_new_entries(feed_cfg, seen, session)` returns `(current_ids, new_entries)` on success or `None` on parse failure (so the caller can skip the `last_run` update and retry on the next cron tick):
  - `current_ids` is *all* IDs currently in the feed (used to overwrite state — old IDs that fall off the feed are forgotten, which keeps `state.json` from growing forever).
  - Entry ID resolution falls back: `entry.id` → `entry.link` → `entry.title`.
  - Unseen entries are reversed into chronological order, then OG images are fetched concurrently (only the first 32KB of each article page is read — the parser stops at `<body>`).
  - Descriptions are HTML-stripped, truncated to 300 chars, and Markdown-escaped (`_MD_ESCAPE_RE` covers `*_` `~` `` ` `` and leading `>`/`#` per line) so feed content can't accidentally format Discord messages.
- `llm.py` — two functions: `run_llm_task(task_cfg)` calls the OpenAI Responses API with the `web_search_preview` built-in tool and posts the plain-text response; `filter_entries(items, filter_cfg, global_model)` is a pure classification call (no web search) that receives a list of `{"id", "title", "description"}` dicts and returns the set of IDs that pass the filter prompt, or `None` on failure. Requires `$OPENAI_API_KEY` in env. Default model: `gpt-5.4-mini`.
- `discord.py` — two posting functions: `post_to_discord` (embed from a `FeedEntry`) and `post_text_to_discord` (plain `content` message, truncated to 2000 chars). Both raise on HTTP ≥400.

### State shape

RSS task entries: `{feed_url: {"items": [{"url": "...", "title": "...", "pass_filter": true|false}, ...], "last_run": "<iso8601 utc>" | null}}`. Each successful run replaces `items` with the items currently in the feed (not a union, so the file size is bounded by feed length). `pass_filter` is only present on items that went through an LLM filter; items from tasks without a `filter` key don't have it.

LLM task entries: `{"<task_name>": {"last_run": "<iso8601 utc>" | null}}`. No `ids` field — only `last_run` matters.

`load_state` returns the parsed JSON as-is; `null` `last_run` always means "due now".

### Config shape

```yaml
tasks:
  # RSS task — detected by presence of 'feeds' key
  - name: my-feeds                   # required for all tasks
    webhook: "https://discord.com/api/webhooks/.../..."
    period: 1                        # optional, hours. Default: 1.0
    filter:                          # optional — LLM pre-post filter for all feeds in this task
      prompt: "Only keep items about AI and machine learning"
      model: gpt-4o-mini             # optional, falls back to global llm.model then default
    feeds:
      - name: "Display name"         # optional, used as embed footer
        url: "https://example.com/feed.xml"

  # LLM task — detected by presence of 'prompt' key
  - name: world-news
    webhook: "https://discord.com/api/webhooks/.../..."
    period: 24
    prompt: "Today news. World. Filter for signal > noise."
    model: gpt-5.4-mini              # optional, default: gpt-5.4-mini
    tools:                           # optional, merged into web_search_preview config
      allowed_domains:
        - reuters.com
      user_location:
        type: approximate
        country: US
```

Multiple `tasks` entries let different groups go to different webhooks. `period` is per-task only. The threshold check subtracts `PERIOD_GRACE` (60s) so a 1h cron firing every ~60min doesn't skip every other tick due to clock jitter. Both YAML and JSON are accepted (dispatched by file suffix in `load_config`).

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-entry, the gather uses `return_exceptions=True` per-feed.
- Keep the 1-second sleep between posts in the same feed (Discord webhook rate limits).
- Don't add a sync HTTP path; the OG-image fetch and feed posting are deliberately concurrent via the shared session.
- Only update `last_run` on a successful feed fetch. A `None` from `get_new_entries` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
