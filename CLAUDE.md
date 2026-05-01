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
- Upgrade `state.json` to the current schema without touching feeds: `uv run claudinho config.yaml --migrate`
- Sync deps: `uv sync`

There is no test suite, linter, or formatter configured.

`config.yaml` is gitignored — copy `config.yaml.template` and fill in webhook URLs and feed URLs. `state.json` is also gitignored and is created next to the config on first non-debug run.

## Architecture

Four modules, no package, flat layout:

- `main.py` — CLI entry point and orchestration. Loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of three modes:
  - **Normal mode**: filters tasks by `_is_due` (period with 60s grace), then gathers all due tasks in parallel. Task type is inferred from config shape: `feeds` key → RSS task, `prompt` key → LLM task.
  - **Debug mode (`--debug`)**: runs the first task sequentially — for LLM tasks posts the full response; for RSS tasks posts the first new entry. Period is ignored. **State is never saved in debug mode.**
  - **Migrate mode (`--migrate`)**: loads `state.json`, runs the legacy-shape migration in `load_state`, writes the result, and exits. Does not open an HTTP session. Mutually exclusive with `--debug`.
- `feed.py` — feed fetching, dedup, and entry enrichment. `get_new_entries(feed_cfg, seen, session)` returns `(current_ids, new_entries)` on success or `None` on parse failure (so the caller can skip the `last_run` update and retry on the next cron tick):
  - `current_ids` is *all* IDs currently in the feed (used to overwrite state — old IDs that fall off the feed are forgotten, which keeps `state.json` from growing forever).
  - Entry ID resolution falls back: `entry.id` → `entry.link` → `entry.title`.
  - Unseen entries are reversed into chronological order, then OG images are fetched concurrently (only the first 32KB of each article page is read — the parser stops at `<body>`).
  - Descriptions are HTML-stripped, truncated to 300 chars, and Markdown-escaped (`_MD_ESCAPE_RE` covers `*_` `~` `` ` `` and leading `>`/`#` per line) so feed content can't accidentally format Discord messages.
- `llm.py` — `run_llm_task(task_cfg)` calls the OpenAI Responses API (`AsyncOpenAI().responses.create`) with the `web_search_preview` built-in tool. Returns the response text or `None` on failure. The `tools` dict in config is shallow-merged into the default `{"type": "web_search_preview"}` so you can add `allowed_domains`, `user_location`, etc. from config without code changes. Requires `$OPENAI_API_KEY` in env. Default model: `gpt-5.4-mini`.
- `discord.py` — two posting functions: `post_to_discord` (embed from a `FeedEntry`) and `post_text_to_discord` (plain `content` message, truncated to 2000 chars). Both raise on HTTP ≥400.

### State shape

RSS task entries: `{feed_url: {"ids": [entry_id, ...], "last_run": "<iso8601 utc>" | null}}`. Each successful run replaces `ids` with the IDs currently in the feed (not a union, so the file size is bounded by feed length).

LLM task entries: `{"llm:<task_name>": {"last_run": "<iso8601 utc>" | null}}`. No `ids` field — only `last_run` matters.

`load_state` transparently migrates the legacy `{feed_url: [entry_id, ...]}` shape: any list value is wrapped as `{"ids": <list>, "last_run": null}`, so a `null` `last_run` always means "due now".

### Config shape

```yaml
tasks:
  # RSS task — detected by presence of 'feeds' key
  - name: my-feeds                   # required for all tasks
    webhook: "https://discord.com/api/webhooks/.../..."
    period: 1                        # optional, hours. Default: 1.0
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
