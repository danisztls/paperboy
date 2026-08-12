# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supported task types:

- **RSS** — polls feeds, posts new entries as Discord embeds, tracks seen entries. A `youtube` pull item is sugar over `feed` (`config.get_feeds` builds the `videos.xml?channel_id=<id>` URL from `channel_id`); everything downstream treats it as a feed.
- **Digest** — like RSS but all passing entries are collected and posted as a single text message (splits on 2000-char limit). No OG image fetching. Uses `[Title](<url>)` to suppress Discord link previews.
- **Real-estate** — structured listings from real-estate portals. vasco's `realestate` adapter fetches (HTTP-first, auto-escalating to Camoufox browser on bot-blocked sites) and parses the source portal (vivareal) into normalized listing dicts; paperboy maps those to `Item`s and applies its own policy (`min_area_per_room`, `max_items`, dedup).
- **Research** — an agentic loop over vasco's real search + `fetch`/`extract` (via vascod): the LLM searches, reads promising pages, then synthesizes a cited plain-text answer. DeepSeek primary, Gemini fallback. No provider `web_search`.
- **Weather** — fetches the daily forecast from Open-Meteo (no API key) and posts a `wttr.in`-style text report. `kind: smart` switches to a signal-only variant gated by σ-based anomaly thresholds against climate normals + past 7 days.
- **Finance** — pulls quotes from yfinance (sync lib wrapped in `asyncio.to_thread`). Detected by `pull` containing a `finance` item with exactly one of two sub-keys: `report` (periodic snapshot) or `monitor` (intraday alerts on deltas + price-band crossings). User writes yfinance symbols verbatim (no alias map).

Each task can push to any combination of targets. Supported targets: `discord` (webhook), `file` (local markdown or JSONL file — extension decides).

Intended to be run on a cron, not as a long-lived process.

## Commands

The project uses `uv` (see `uv.lock`, `.python-version` pinning Python 3.14).

- Run: `uv run main.py` (reads `~/.config/paperboy/config.yaml`, writes state to `~/.local/share/paperboy/state.json`)
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

After any implementation, run format then lint before finishing.

Config is read from `$XDG_CONFIG_HOME/paperboy/config.yaml` (default `~/.config/paperboy/config.yaml`) and state is written to `$XDG_DATA_HOME/paperboy/state.json` (default `~/.local/share/paperboy/state.json`). Both paths can be overridden: pass `--config` and/or `--state`. Copy `config/config.yaml.template` and fill in webhook URLs and feed URLs.

Logs are written to `<state_dir>/logs/<timestamp>.log` on every run.

Eval traces (every LLM call's prompt, response, tokens, latency, optional reasoning) are written to `<state_dir>/evals/<task_name>/<run_iso>.jsonl` on every run — one record per LLM call. No rotation policy ships yet; clean up manually if disk pressure becomes an issue.

## Architecture

Slim root modules (`main.py`, `logsetup.py`, `pipeline.py`, `stats.py`, `util.py`) plus subpackages; orchestration lives in the `tasks/` package. The execution model is a **pull → process → push** pipeline, with always-on capture writing every LLM call's I/O to disk:

```
Source.pull()  →  process.summarize_items() + process.curate_items()  →  Target.push()
```

### Root modules

- `main.py` — CLI entry point. Parses args, then dispatches: one-shot modes (`--validate`, `--stats`, `--summarize`, `--get-content`) or `_async_main` (lock file, config+state load, auto-migration, retention pruning, `--clean`/`--migrate`/`--regenerate-state`, or the normal run-due-tasks-in-parallel path). Builds one `RunContext` (shared `aiohttp.ClientSession`, full config, `LLMHandles`, always-on `RunCapture` collector, `analysis` flag) and hands every due task to `tasks.processor_for(kind)`. After tasks finish, captured LLM calls are flushed to `<state_dir>/evals/<task>/<run_iso>.jsonl`. `--analysis` reshapes the run into "expensive inspection mode" (reasoning on, ELI5 filter reasons, item/feed truncation, dry-run, render to stdout). Public helpers (tested): `merge_task_results`, `prune_old_files`.
- `logsetup.py` — logging configuration: journald / rich-tty / plain handlers picked by environment (`setup(verbose=)`), per-run DEBUG file log (`add_file_handler`), `APP_LOGGERS`, third-party noise silencing.
- `pipeline.py` — `Source` / `Target` ABCs and data types: `Item`, `PullResult` (with optional `name`), `CurateResult` (carries `coverage`), `CoverageUpdate` (topic ledger update + digest paragraph), `MemoryParagraph` (`text` + `citations: list[int]`, the digest-render type), `PushContext`. To add a source (e.g. Reddit, YouTube), implement `Source`. To add a target (Telegram, email), implement `Target` — no changes to task orchestration needed.
- `stats.py` — `print_stats(config, state)` builds a Rich table of per-task and per-source state (kind, period, last_run, estimated next_run, item counts) for `--stats` mode. Pure read-only: no network, no LLM, no state writes. `humanize_minutes` lives here (also used by `main._log_not_due`).
- `util.py` — `utc_now_iso()` (the second-precision ISO timestamp used throughout state).
- `constants.py` — `USER_AGENT`.

### Subpackages

Each has its own `CLAUDE.md` with details:

- [`tasks/CLAUDE.md`](tasks/CLAUDE.md) — task orchestration: `process_*_task` per kind, `RunContext`, due checks
- [`pull/CLAUDE.md`](pull/CLAUDE.md) — source implementations (RSS, research, realestate, weather, finance)
- [`push/CLAUDE.md`](push/CLAUDE.md) — Discord + file target implementations
- [`process/CLAUDE.md`](process/CLAUDE.md) — curate (LLM), summarize (LLM), filter_heuristic (regex), vascod client
- [`providers/llm/CLAUDE.md`](providers/llm/CLAUDE.md) — provider adapters, `ModelHandle`, `ModelSpec` capability registry
- [`state/CLAUDE.md`](state/CLAUDE.md) — state I/O and schema migrations
- [`config/CLAUDE.md`](config/CLAUDE.md) — config loading and validation
- [`evals/CLAUDE.md`](evals/CLAUDE.md) — captured LLM-call traces
- [`tests/CLAUDE.md`](tests/CLAUDE.md) — test approach, fixtures, and what's covered
- [`benchmark/CLAUDE.md`](benchmark/CLAUDE.md) — standalone benchmark script

## State shape

State is keyed by task name under a top-level `"tasks"` key. Meta keys live at the top level:

```json
{
  "_version": 7,
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
      "coverage": {
        "ledger": [
          {"id": "us-iran-war", "label": "US–Iran war & ceasefire", "state": "Latest factual state…", "first_seen": "<iso8601 utc>", "last_seen": "<iso8601 utc>", "frequency": 7}
        ],
        "rollups": [
          {"period": "2026-05", "topics": [{"id": "...", "label": "...", "state": "...", "frequency": 9}]}
        ]
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
      "realestate": {
        "https://www.vivareal.com.br/aluguel/...": {"last_run": "<iso8601 utc>", "items": [...]},
        "__legacy__": {"items": [...]}
      }
    }
  }
}
```

- `feeds` — per-URL state. `name` resolved from cfg → feed `<title>` → url. `items` is replaced on each successful fetch (bounded by feed length). `first_seen` stamped when an item is first seen and carried forward; `source_date` is the entry's original pubDate. `filter_pass` / `filter_reason` only present on items from tasks with a `curate` key.
- `coverage` — only on curated tasks (digest and non-digest alike; curation is identical, only presentation differs). `coverage.ledger` is a **topic-keyed** list: each entry is a topic the curator has covered, with `state` (latest factual state — the digest paragraph when a NEW topic is touched; continuing topics render the one-sentence `CoverageUpdate.update` delta instead, while the ledger keeps storing the full `state`), `frequency` (times covered, code-maintained), and `first_seen`/`last_seen`. Step 3 of curate emits one `CoverageUpdate` per passing topic (continue an existing `id` or start a new one); `feed_state.apply_coverage` upserts them, bumps `frequency`, evicts topics dormant past 21 days, and caps at 60. The whole active ledger is fed back as Step-2 dedup + escalating-trajectory context (the bar reads `frequency` directly, tiered — freq 8+ passes only reversals/resolutions/ruptures — with a daily cap: a topic already touched today is held to the freq 8+ bar). Topics dormant past 21 days leave the active ledger and fold into `coverage.rollups` — per-month buckets keeping the top 15 topics by frequency, last 6 months. Rollups are the long-horizon "cutoff-gap" backdrop: fed to the model as `## Background` (significance + resurfacing context only — never deduped against). Replaces the old prose `memory` log (v7 migration drops it).
- `climate` — only on `kind: smart` weather tasks. Monthly cache (μ + σ for apparent max/min and daily-mean humidity over the current calendar month across the last `CLIMATE_NORMAL_YEARS` years). Refreshed on month rollover. Pre-σ caches (written before the σ rollout) are silently treated as stale (`apparent_max_std` absent) and force a single refetch.
- `tickers` — only on finance `monitor` tasks. Per-ticker `last_price` is the baseline for the next tick's delta check; `band_side` (`"in" | "above" | "below"`) is present only when the rule sets a `price:` band and gates band-crossing dedup. First-ever run for a ticker only records the baseline — no alert fires until the next tick. Report-mode finance tasks store only `last_run`.
- `realestate` — only on real-estate tasks (named `scrapers` in v4/v5; renamed in v6). Keyed by source `url` → `{items, last_run}`. Each successful source pull replaces its own `items` (bounded by listings on the page) and stamps its own `last_run`. Task-level `last_run` is the latest among sources. The `__legacy__` bucket (from the v3→v4 migration) contributes URLs to every source's `seen` set for dedup but is never written to; preserved by `--clean` so it can shrink only as URLs cycle out of other sources' coverage.
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
- Don't add a sync HTTP path; feed fetching runs concurrently via the shared aiohttp session. Article content extraction (summarize step) is handled by `vascod` (the resident vasco daemon, sibling project) over a UNIX socket — paperboy is a thin client (`process/_vasco.py`), **not** a vasco library importer. vascod must be running (`systemctl --user status vascod.service`); a failed fetch returns `None` and the item is skipped this run. See `process/CLAUDE.md`.
- Only update `last_run` on a successful feed fetch. A `None` from `Source.pull()` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
- LLM curate failures retry once after 10s; on second failure all items are treated as passing (fail-open).
- All feeds in a given RSS/digest task share one LLM curate call; items are sent grouped by source with monotonically increasing integer IDs across all feeds.
- `Item.meta` carries per-item display hints (e.g. `color`) set during the pull stage so the target doesn't need to re-resolve them from config.
- `Item.image` is the single-image path (RSS, og:image, search); `Item.images` is the multi-image path (real-estate sources). When both are set, `image` should be `images[0]`. `DiscordEmbedTarget` prefers `images`, capping at 4 (Discord's embed-merge limit) and degrading to a single embed when `Item.url` is missing.
- Feed-level `curate.skip: true` short-circuits the LLM curate call for that feed only — items always pass. Useful for trusted, low-volume feeds where the curate cost isn't justified. Other feeds in the same task still get curated normally.
- Heuristic per-feed processing is four layered blocks resolved global→task→feed by `config.scope.resolve_scoped` (over `layer_dict`): `ignore` (omit a FIELD — `image`, `description`), `skip` (omit an ENTRY — `shorts`, `livestreams`, `url_contains`), and `description`/`title` (regex transforms `remove`/`extract`/`replace` via `process.filter_heuristic.apply_regex`). The `youtube:` scope block reuses the `ignore`/`skip` vocabulary but applies **only to YouTube feeds** (gated by `is_youtube_feed_url`), so e.g. a global `youtube.ignore.description: true` clears every YouTube description without touching other feeds. `skip.shorts` is a free `/shorts/` URL check; `skip.livestreams` fetches each new `/watch` page directly (not via vascod, which returns transcripts) to read `"isLiveContent"` — fail-open, runs last on survivors of the cheaper filters; both self-gate to YouTube URLs. See `pull/CLAUDE.md`.
- `--analysis` forces reasoning on (passes `reasoning=True` to every adapter), overriding any per-spec `ModelSpec.reasoning` value. Normal cron runs honor the per-spec value. `ModelHandle.reasoning_for(analysis)` (`providers/llm/base.py`) encodes this precedence.
- `period`'s suffix decides the comparison kind, not just the magnitude: `Nm`/`Nh` are sliding-window durations (`(now - last) >= period`), while `Nd`/`Nw` are calendar-aligned and fire on the next sweep after the local date / ISO week has advanced. `parse_period` returns a `Period` dataclass (`config.Period`); `is_due` in `tasks/due.py` branches on `period.is_calendar`. Calendar units use system local time (`dt.astimezone()`) so morning cron sweeps fire as soon as the date rolls over.
- A feed may carry its own `period:` overriding the task period (`tasks.due.due_feeds`); absent → it inherits the task period (itself defaulting to `DEFAULT_PERIOD` 1h). The task wakes on the **shortest** of its feeds' periods (`task_is_due` is "any feed due at its own period"), and `process_feed_task` fetches only the feeds whose own clock elapsed — not-due feeds keep their prior state (incl. `last_run`) untouched because `build_feed_task_state` only overwrites the feeds it processed. Bypassed for `--task` (`RunContext.force`) and `--analysis`. **Digest tasks reject per-feed `period:`** (validation error) and never get per-feed gating — a digest posts all feeds in one message, so cadences can't be split.
- When changing architecture (new/renamed modules, classes, or functions), CLI flags, config keys, or state shape, update the relevant `CLAUDE.md` in the same commit — root for cross-cutting changes, subpackage `CLAUDE.md` for local ones. The existing rule about config-key changes updating the Pydantic model is the same idea, extended to docs.
