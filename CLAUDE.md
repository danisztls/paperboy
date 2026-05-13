# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supports four task types:
- **RSS tasks**: polls feeds, posts new entries as Discord embeds, tracks seen entries.
- **Digest tasks**: like RSS tasks but all passing entries are collected and posted as a single text message (splits on 2000-char limit). No OG image fetching. Uses `[Title](<url>)` to suppress Discord link previews.
- **Scraper tasks**: browser-based extraction from JavaScript-heavy sites via Playwright; posts new listings as Discord embeds.
- **LLM tasks**: calls a configurable LLM (OpenAI or Gemini) with a prompt + web search, posts the plain-text response. Good for scheduled digests ("today's news, filter for signal > noise").

Each task can push to any combination of targets. Supported targets: `discord` (webhook), `file` (local markdown file).

Intended to be run on a cron, not as a long-lived process.

## Commands

The project uses `uv` (see `uv.lock`, `.python-version` pinning Python 3.14).

- Run: `uv run main.py` (reads `~/.config/claudinho/config.yaml`, writes state to `~/.local/share/claudinho/state.json`)
- Run with explicit config: `uv run main.py --config config.yaml` (state defaults to `<config_dir>/state.json`)
- Run one task by name, ignoring period/last_run: `uv run main.py --task "world-news"`
- Verbose output: add `--verbose` to any invocation
- Validate config and exit: `uv run main.py --validate`
- Migrate state to current schema version: `uv run main.py --migrate`
- Clean stale state entries and exit: `uv run main.py --clean`
- Summarize a YouTube video to stdout: `uv run main.py --summarize <url>`
- Sync deps: `uv sync`
- Format: `uv run ruff format .`
- Lint: `uv run ruff check --fix .`

After any implementation, run format then lint before finishing.

Config is read from `$XDG_CONFIG_HOME/claudinho/config.yaml` (default `~/.config/claudinho/config.yaml`) and state is written to `$XDG_DATA_HOME/claudinho/state.json` (default `~/.local/share/claudinho/state.json`). Both paths can be overridden: pass `--config` and/or `--state`. Copy `config.yaml.template` and fill in webhook URLs and feed URLs.

Logs are written to `<state_dir>/logs/<timestamp>.log` on every run.

## Architecture

Eight root modules plus three subpackages (`pull/`, `push/`, `llm/`). The execution model follows a **pull → process → push** pipeline:

```
Source.pull()  →  _summarize_items() + _apply_llm_filter()  →  Target.push()
```

### Pipeline abstractions (`pipeline.py`)

Defines the interfaces that all sources and targets implement:

- **`Item`** — generic content item produced by any source. Fields: `id`, `title`, `source` (display name), `url`, `body` (sanitized text), `image`, `published`, `summary`, `filter_pass`, `filter_reason`, `meta` (dict for source-specific extras).
- **`PullResult`** — output of `Source.pull()`: `new_items: list[Item]` + `current_items: list[dict]` (url+title dicts for state).
- **`FilterResult`** — output of the LLM filter step: `items` (all items with filter_pass set), `memory` (new briefing text), `cite_map` (LLM int ID → (source, url)).
- **`PushContext`** — input to `Target.push()`: `items`, optional `memory`, optional `cite_map`.
- **`Source(ABC)`** — one abstract method: `pull(cfg, seen, session) → PullResult | None`. Return `None` on failure; the caller must not update state.
- **`Target(ABC)`** — one abstract method: `push(ctx, cfg, session) → set[str]`. Returns IDs of items that failed to publish.

To add a new source (e.g. Reddit, YouTube), implement `Source`. To add a new target (Telegram, email), implement `Target` — no changes to task orchestration needed.

### Module overview

- `main.py` — CLI entry point and orchestration. Resolves config/state paths, manages a lock file, loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of five modes: normal (run due tasks in parallel), `--regenerate-state`, `--clean`/`--migrate`, `--validate`, or `--summarize`.
- `config.py` — config loading and validation. `load_config(path)` reads YAML or JSON. `validate_config(config)` uses Pydantic models to validate the full config and returns a list of error strings. Also houses `_parse_color`, `_parse_period`, `_task_type` (returns the explicit `type:` key if present; otherwise infers from `pull` list: `scraper` item → scraper, `llm` item → LLM, else feeds), `_resolve_model_spec` (returns `(provider, model_name)` from a `{provider, model}` dict), `_get_api_key_for_provider`, and helpers: `_get_feeds`, `_get_discord_cfg`, `_get_llm_pull_cfg`, `_get_scraper_cfg`, `_get_file_path`.
- `state.py` — state I/O and maintenance. `load_state`, `save_state` (writes `.old` backup, stamps `_version` and `_last_run`), `_auto_clean` (removes malformed items), and `_remove_unknown` (prunes tasks/feeds absent from config).
- `migrate.py` — state schema migrations. `needs_migration(state)` checks `state["_version"]` against `CURRENT_VERSION` (2). `migrate(state)` steps through `_STEPS` until current. The v2 migration nests all task keys under a top-level `"tasks"` key.
- `tasks.py` — task orchestration. Implements the pipeline stages:
  - `_pull_feeds(source, feed_cfgs, feeds_state, task_filter, session)` — fetches all feeds concurrently via `RSSSource`, merges heuristic filters, returns `{url: PullResult | None}`.
  - `_summarize_items(items, ...)` — concurrently fetches item content and summarizes via LLM; sets `item.summary`.
  - `_apply_llm_filter(items, filter_cfg, ...)` — groups items by source, calls `filter_entries`, maps results back onto items as `filter_pass`/`filter_reason`, retries once on failure; returns `FilterResult`.
  - `_process_llm_search_task` — LLM web-search pipeline: `LLMSearchSource.pull()` → `DiscordTextTarget.push()` (+ `FileEmbedTarget` if configured).
  - `_process_llm_evaluate_task` — RSS/digest pipeline: pull → summarize → filter → `DiscordEmbedTarget`, `DiscordMarkdownTarget`, or `DiscordDigestTarget` (+ file target if configured) → state update.
  - `_process_scraper_task` — scraper pipeline: `ScraperSource.pull()` → `DiscordEmbedTarget` or `DiscordMarkdownTarget` → state update.
  - `_is_due` checks period with 60s grace. `_merge_filter` combines task-level and feed-level heuristic filter dicts.
- `llm_filter.py` — LLM-based feed entry classifier. `filter_entries(items, filter_cfg, ...)` classifies items grouped by source; returns `(results_dict, memory_text) | None`. `results_dict` maps `str(id)` → `{"pass": bool, "reason": str}`; `memory_text` is the new memory log entry. When `explain: true`, passing-item reasons are ELI5-style (2–3 sentences). Uses the LLM adapter resolved from config.
- `summarize.py` — LLM summarization helpers. `summarize_entry(title, body, adapter, ...)` summarizes a feed entry. `summarize_transcript(transcript, adapter, ...)` summarizes a YouTube transcript. `fetch_item_content(url, session)` fetches and extracts readable text from an article URL.

#### `pull/` — source implementations

- `pull/feed.py` — feed fetching, dedup, and entry enrichment.
  - `RSSSource(Source)` — concrete source; wraps `get_new_entries`.
  - `get_new_entries(feed_cfg, seen, session)` — fetches and parses the feed, returns `(current_items, new_entries: list[Item])` or `None` on parse failure. Entry ID is `entry.link`; entries with no link or older than 7 days are skipped. Bodies are HTML-stripped, truncated to 512 chars, and Markdown-escaped. Heuristic filters (`filter.title`, `filter.description`, `filter.url`) are applied via `_apply_regex`. Supported ops: `extract` (regex), `replace`/`with`, `remove_phrases_with_urls`, `remove_phrases_containing`, `clear`, `skip_containing`.
- `pull/llm.py` — LLM web-search source.
  - `LLMSearchSource(Source)` — calls `run_llm_task` and wraps the response as a single `Item`.
  - `run_llm_task(task_cfg, instructions, model, adapter)` — calls the configured LLM with web search enabled, returns plain-text response or `None`.
- `pull/scraper.py` — browser-based extraction via Playwright.
  - `ScraperSource(Source)` — launches a headless Chromium browser, delegates to a site adapter, returns new listings as `Item`s.
- `pull/adapters/vivareal.py` — `VivaRealAdapter`: parses property listings from VivaReal search pages.

#### `push/` — target implementations

- `push/discord.py` — Discord webhook targets and underlying posting functions.
  - `DiscordEmbedTarget(Target)` — posts each item as a Discord embed with optional OG image fetch/download.
  - `DiscordTextTarget(Target)` — posts each item's body as a plain text message (truncated to 2000 chars).
  - `DiscordMarkdownTarget(Target)` — posts each item as `### [title](<url>) source\nbody` (markdown format, no embed).
  - `DiscordDigestTarget(Target)` — posts `ctx.memory` as ≤2000-char chunks with `[n]` citation markers replaced by `[[Source]](<url>)` Discord masked links.
  - All use `_post_webhook` which retries once on 429. OG image fetching retries once after 2s on bot-detection (response < 2 KB).
- `push/file.py` — file-based target implementations. Path is expanded (`~`, env vars) and parent dirs are created on first write.
  - `FileEmbedTarget(Target)` — appends each item as `## [Title](url)\n*source · date*\n\nbody\n\n---` blocks to the configured file.
  - `FileDigestTarget(Target)` — appends `## YYYY-MM-DD\n\ndigest text\n\n---` to the configured file, with `[n]` citation markers resolved to standard markdown `[Source](url)` links.

#### `llm/` — LLM provider adapters

- `llm/__init__.py` — `get_adapter(provider, api_key)` factory; returns an `OpenAIAdapter` or `GeminiAdapter`.
- `llm/adapters/base.py` — `LLMAdapter` ABC with a single `complete(messages, ...)` method.
- `llm/adapters/openai.py` — OpenAI adapter (Responses API, supports `web_search_preview`).
- `llm/adapters/gemini.py` — Gemini adapter.

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
- Only update `last_run` on a successful feed fetch. A `None` from `Source.pull()` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
- LLM filter failures retry once after 10s; on second failure all items are treated as passing (fail-open).
- All feeds in a given RSS/digest task share one LLM filter call; items are sent grouped by source with monotonically increasing integer IDs across all feeds.
- `Item.meta` carries per-item display hints (e.g. `color`, `download_og`) set during the pull stage so the target doesn't need to re-resolve them from config.
