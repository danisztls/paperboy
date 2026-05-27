# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supported task types:

- **RSS** — polls feeds, posts new entries as Discord embeds, tracks seen entries.
- **Digest** — like RSS but all passing entries are collected and posted as a single text message (splits on 2000-char limit). No OG image fetching. Uses `[Title](<url>)` to suppress Discord link previews.
- **Scraper** — structured extraction from listing sites. Pages are fetched via vasco (HTTP-first with auto-escalation to Camoufox browser on bot-blocked sites), then parsed with BeautifulSoup. Site-specific adapters extract fields like price, bedrooms, images.
- **Search** — calls a configurable LLM (OpenAI, Gemini, Anthropic, or DeepSeek) with a prompt + web search, posts the plain-text response.
- **Weather** — fetches the daily forecast from Open-Meteo (no API key) and posts a `wttr.in`-style text report. `kind: smart` switches to a signal-only variant gated by σ-based anomaly thresholds against climate normals + past 7 days.
- **Finance** — pulls quotes from yfinance (sync lib wrapped in `asyncio.to_thread`). Detected by `pull` containing a `finance` item with exactly one of two sub-keys: `report` (periodic snapshot) or `monitor` (intraday alerts on deltas + price-band crossings). User writes yfinance symbols verbatim (no alias map).

Each task can push to any combination of targets. Supported targets: `discord` (webhook), `file` (local markdown or JSONL file — extension decides).

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
- Print a rich-formatted summary of state.json: `uv run main.py --stats`
- Summarize a YouTube video to stdout: `uv run main.py --summarize <url>`
- Print article text or YouTube transcript to stdout without summarizing: `uv run main.py --get-content <url>`
- Sync deps: `uv sync`
- Format: `uv run ruff format .`
- Lint: `uv run ruff check --fix .`
- Run tests: `uv run pytest`
- Run benchmark: `uv run benchmark/` (reads `benchmark/config.yaml`, writes JSON to `benchmark/results/`)
- Inspect a run with chain-of-thought + ELI5 filter reasons (extra tokens, dry-run): `uv run main.py --analysis --task <name>`
  - `--analysis-limit-items N` (default 7, 0 = unlimited): cap entries per feed
  - `--analysis-limit-feeds N` (default 7, 0 = unlimited): cap feeds per task
  - `--human`: render rich/human-readable output to stdout instead of JSON
- Replay captured LLM calls against alternative models: `uv run main.py --replay <state_dir>/evals/<task>/<run_iso>.jsonl --models openai:gpt-4o-mini,gemini:gemini-2.5-flash --call filter`

After any implementation, run format then lint before finishing.

Config is read from `$XDG_CONFIG_HOME/claudinho/config.yaml` (default `~/.config/claudinho/config.yaml`) and state is written to `$XDG_DATA_HOME/claudinho/state.json` (default `~/.local/share/claudinho/state.json`). Both paths can be overridden: pass `--config` and/or `--state`. Copy `config/config.yaml.template` and fill in webhook URLs and feed URLs.

Logs are written to `<state_dir>/logs/<timestamp>.log` on every run.

Eval traces (every LLM call's prompt, response, tokens, latency, optional reasoning) are written to `<state_dir>/evals/<task_name>/<run_iso>.jsonl` on every run — one record per LLM call. Replay output goes to `<state_dir>/evals/replays/<basename>__replay_<ts>.json`. No rotation policy ships yet; clean up manually if disk pressure becomes an issue.

## Architecture

Four root modules (`main.py`, `tasks.py`, `pipeline.py`, `stats.py`) plus subpackages. The execution model is a **pull → process → push** pipeline, with always-on capture writing every LLM call's I/O to disk:

```
Source.pull()  →  _summarize_items() + _apply_curate()  →  Target.push()
```

### Root modules

- `main.py` — CLI entry point and orchestration. Resolves config/state paths, manages a lock file, loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of these short-circuit modes (`--regenerate-state`, `--clean`/`--migrate`, `--validate`, `--summarize`, `--replay`) or the normal run-due-tasks-in-parallel path. A `RunCapture` is constructed unconditionally; after tasks finish, captured LLM calls are flushed to `<state_dir>/evals/<task>/<run_iso>.jsonl`. `--analysis` reshapes the run into "expensive inspection mode" (reasoning on, ELI5 filter reasons, item/feed truncation, dry-run, render to stdout).
- `tasks.py` — task orchestration. `_process_feed_task`, `_process_search_task`, `_process_scraper_task`, `_process_weather_task`, `_process_finance_task`. Helpers: `_pull_feeds`, `_summarize_items`, `_apply_curate`, `_is_due`, `_merge_filter`, `_effective_reasoning`. Every LLM-touching stage takes a `collector=` (always-on `RunCapture`) and an `analysis: bool = False`. `analysis` controls dry-run, item/feed truncation, `reasoning=True` on adapter calls, and forcing `explain=True` on the filter prompt; the collector controls only what gets recorded.
- `pipeline.py` — `Source` / `Target` ABCs and data types: `Item`, `PullResult` (with optional `name`), `FilterResult`, `MemoryParagraph` (`text` + `citations: list[int]`), `PushContext`. To add a source (e.g. Reddit, YouTube), implement `Source`. To add a target (Telegram, email), implement `Target` — no changes to task orchestration needed.
- `stats.py` — `print_stats(config, state)` builds a Rich table of per-task and per-source state (kind, period, last_run, estimated next_run, item counts) for `--stats` mode. Pure read-only: no network, no LLM, no state writes. `_humanize_minutes` and `_humanize_delta` live here; `main.py`'s `_check_due_or_skip` imports `_humanize_minutes` from this module.

### Subpackages

Each has its own `CLAUDE.md` with details:

- [`pull/CLAUDE.md`](pull/CLAUDE.md) — source implementations (RSS, search, scraper, weather, finance)
- [`push/CLAUDE.md`](push/CLAUDE.md) — Discord + file target implementations
- [`process/CLAUDE.md`](process/CLAUDE.md) — curate (LLM), filter_heuristic (regex), summarize
- [`providers/llm/CLAUDE.md`](providers/llm/CLAUDE.md) — provider adapters and `ModelSpec` capability registry
- [`state/CLAUDE.md`](state/CLAUDE.md) — state I/O and schema migrations
- [`config/CLAUDE.md`](config/CLAUDE.md) — config loading and validation
- [`evals/CLAUDE.md`](evals/CLAUDE.md) — captured LLM-call traces and replay
- [`tests/CLAUDE.md`](tests/CLAUDE.md) — test approach, fixtures, and what's covered
- [`benchmark/CLAUDE.md`](benchmark/CLAUDE.md) — standalone benchmark script

## State shape

State is keyed by task name under a top-level `"tasks"` key. Meta keys live at the top level:

```json
{
  "_version": 4,
  "_last_run": "<iso8601 utc>",
  "_last_clean": "<iso8601 utc>",
  "tasks": {
    "my-feeds": {
      "feeds": {
        "https://feed1.url": {
          "name": "Feed 1",
          "items": [
            {"url": "...", "title": "...", "source_date": "<iso8601 utc>", "first_seen": "<iso8601 utc>", "filter_pass": true, "filter_reason": "..."}
          ],
          "last_run": "<iso8601 utc>"
        }
      },
      "memory": {
        "2026-05-01T12:00:00Z": "Recurring themes this week include..."
      }
    },
    "world-news": {"last_run": "<iso8601 utc>"},
    "weather-smart": {
      "last_run": "<iso8601 utc>",
      "climate": {
        "month": "2026-05",
        "fetched_at": "<iso8601 utc>",
        "apparent_max_mean": 27.4, "apparent_max_std": 2.1,
        "apparent_min_mean": 17.8, "apparent_min_std": 1.8,
        "humidity_mean": 72.0, "humidity_std": 8.0
      }
    },
    "finance-monitor": {
      "last_run": "<iso8601 utc>",
      "tickers": {
        "AAPL": {"last_price": 220.50},
        "NVDA": {"last_price": 955.20, "band_side": "above"}
      }
    },
    "imoveis": {
      "last_run": "<iso8601 utc>",
      "scrapers": {
        "vivareal": {"last_run": "<iso8601 utc>", "items": [...]},
        "__legacy__": {"items": [...]}
      }
    }
  }
}
```

- `feeds` — per-URL state. `name` resolved from cfg → feed `<title>` → url. `items` is replaced on each successful fetch (bounded by feed length). `first_seen` stamped when an item is first seen and carried forward; `source_date` is the entry's original pubDate. `filter_pass` / `filter_reason` only present on items from tasks with a `curate` key.
- `memory` — only on curated RSS/digest tasks. Each run appends one entry keyed by ISO8601 timestamp; history capped at 20 entries (oldest evicted). The LLM receives the last 5 entries as context on each run.
- `climate` — only on `kind: smart` weather tasks. Monthly cache (μ + σ for apparent max/min and daily-mean humidity over the current calendar month across the last `CLIMATE_NORMAL_YEARS` years). Refreshed on month rollover. Pre-σ caches (written before the σ rollout) are silently treated as stale (`apparent_max_std` absent) and force a single refetch.
- `tickers` — only on finance `monitor` tasks. Per-ticker `last_price` is the baseline for the next tick's delta check; `band_side` (`"in" | "above" | "below"`) is present only when the rule sets a `price:` band and gates band-crossing dedup. First-ever run for a ticker only records the baseline — no alert fires until the next tick. Report-mode finance tasks store only `last_run`.
- `scrapers` — only on scraper tasks (v4+). Adapter → `{items, last_run}`. Each successful adapter pull replaces its own `items` (bounded by listings on the page) and stamps its own `last_run`. Task-level `last_run` is the latest among adapters. The `__legacy__` bucket (from v3→v4 migration) contributes URLs to every adapter's `seen` set for dedup but is never written to; preserved by `--clean` so it can shrink only as URLs cycle out of other adapters' coverage.
- Search tasks store only `last_run` directly under the task name key.
- `load_state` returns the parsed JSON as-is; absent or `null` `last_run` always means "due now".
- `_last_clean` (top-level) is reserved as a meta key (recognized by `state/migrate.py`) and appears in the template, but no code path currently writes it.
- Use `--migrate` to update an old state file to the current schema. Use `--regenerate-state` to rebuild from scratch.

## Config shape

See `config/config.yaml.template` — canonical reference for all supported keys and defaults.

Any change that adds, removes, or renames a config key must also update the corresponding Pydantic model in `config/__init__.py` so validation stays in sync.

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-task exceptions, the gather uses `return_exceptions=True`.
- Keep the 2-second sleep between posts in the same task (Discord webhook rate limits).
- Don't add a sync HTTP path; feed fetching runs concurrently via the shared aiohttp session. Article content extraction (summarize step) is handled by `vasco` (sibling project, library dep) which uses httpx internally with auto-mode escalation and SQLite caching.
- Only update `last_run` on a successful feed fetch. A `None` from `Source.pull()` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
- LLM curate failures retry once after 10s; on second failure all items are treated as passing (fail-open).
- All feeds in a given RSS/digest task share one LLM curate call; items are sent grouped by source with monotonically increasing integer IDs across all feeds.
- `Item.meta` carries per-item display hints (e.g. `color`) set during the pull stage so the target doesn't need to re-resolve them from config.
- `Item.image` is the single-image path (RSS, og:image, search); `Item.images` is the multi-image path (real-estate scrapers). When both are set, `image` should be `images[0]`. `DiscordEmbedTarget` prefers `images`, capping at 4 (Discord's embed-merge limit) and degrading to a single embed when `Item.url` is missing.
- `web_search` is plumbed through the curate call but intentionally not through `summarize_entry`: summaries run on already-fetched article content, so extra web search is wasted tokens.
- Feed-level `curate.skip: true` short-circuits the LLM curate call for that feed only — items always pass. Useful for trusted, low-volume feeds where the curate cost isn't justified. Other feeds in the same task still get curated normally.
- `--analysis` forces reasoning on (passes `reasoning=True` to every adapter), overriding any per-spec `ModelSpec.reasoning` value. Normal cron runs honor the per-spec value. The `_effective_reasoning(default, analysis)` helper in `tasks.py` encodes this precedence.
- `period`'s suffix decides the comparison kind, not just the magnitude: `Nm`/`Nh` are sliding-window durations (`(now - last) >= period`), while `Nd`/`Nw` are calendar-aligned and fire on the next sweep after the local date / ISO week has advanced. `parse_period` returns a `Period` dataclass (`config.Period`); `_is_due` in `tasks.py` branches on `period.is_calendar`. Calendar units use system local time (`dt.astimezone()`) so morning cron sweeps fire as soon as the date rolls over.
- When changing architecture (new/renamed modules, classes, or functions), CLI flags, config keys, or state shape, update the relevant `CLAUDE.md` in the same commit — root for cross-cutting changes, subpackage `CLAUDE.md` for local ones. The existing rule about config-key changes updating the Pydantic model is the same idea, extended to docs.
