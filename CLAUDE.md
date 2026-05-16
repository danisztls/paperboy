# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal notifier that posts to Discord webhooks on a cron schedule. Supports four task types:
- **RSS tasks**: polls feeds, posts new entries as Discord embeds, tracks seen entries.
- **Digest tasks**: like RSS tasks but all passing entries are collected and posted as a single text message (splits on 2000-char limit). No OG image fetching. Uses `[Title](<url>)` to suppress Discord link previews.
- **Scraper tasks**: browser-based extraction from JavaScript-heavy sites via Camoufox (hardened Firefox, drop-in Playwright API); posts new listings as Discord embeds. One-time setup: `uv run camoufox fetch` to download the ~700MB browser binary.
- **Search tasks**: calls a configurable LLM (OpenAI or Gemini) with a prompt + web search, posts the plain-text response. Good for scheduled digests ("today's news, filter for signal > noise").
- **Weather tasks**: fetches the daily forecast from Open-Meteo (no API key) and posts a `wttr.in`-style plain-text report with emoji: today's min/max/avg, apparent-temperature hourly curve, unsafe UV window, rain mm + probability; followed by compact per-day lines for upcoming days. Detected by `pull` containing a `weather` item. Must have a Discord push target.

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

Three root modules (`main.py`, `tasks.py`, `pipeline.py`) plus subpackages (`pull/`, `push/`, `process/`, `providers/llm/`, `state/`, `config/`, `evals/`, `benchmark/`). The execution model follows a **pull → process → push** pipeline, with always-on capture writing every LLM call's I/O to disk:

```
Source.pull()  →  _summarize_items() + _apply_curate()  →  Target.push()
```

### Pipeline abstractions (`pipeline.py`)

Defines the interfaces that all sources and targets implement:

- **`Item`** — generic content item produced by any source. Fields: `id`, `title`, `source` (display name), `url`, `body` (sanitized text), `image`, `published`, `summary`, `filter_pass`, `filter_reason`, `meta` (dict for source-specific extras).
- **`PullResult`** — output of `Source.pull()`: `new_items: list[Item]` + `current_items: list[dict]` (url/title/optional `source_date` dicts for state) + optional `name` (display name of the source; set by feed sources, lands on the feed dict in state).
- **`FilterResult`** — output of the LLM curate step: `items` (all items with filter_pass set), `memory: list[MemoryParagraph] | None` (new briefing as structured paragraphs), `cite_map: dict[int, Citation]` (LLM int ID → `Citation(source, url)` NamedTuple).
- **`MemoryParagraph`** — one paragraph of the digest briefing: `text: str` (plain prose, no citation markers) + `citations: list[int]` (item IDs that support this paragraph). Push targets resolve IDs via `cite_map` and append source links after the text.
- **`PushContext`** — input to `Target.push()`: `items`, optional `memory: list[MemoryParagraph]`, optional `cite_map`.
- **`Source(ABC)`** — one abstract method: `pull(cfg, seen, session) → PullResult | None`. Return `None` on failure; the caller must not update state.
- **`Target(ABC)`** — one abstract method: `push(ctx, cfg, session) → set[str]`. Returns IDs of items that failed to publish.

To add a new source (e.g. Reddit, YouTube), implement `Source`. To add a new target (Telegram, email), implement `Target` — no changes to task orchestration needed.

### Module overview

- `main.py` — CLI entry point and orchestration. Resolves config/state paths, manages a lock file, loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of these short-circuit modes (`--regenerate-state`, `--clean`/`--migrate`, `--validate`, `--summarize`, `--replay`) or the normal run-due-tasks-in-parallel path. A `RunCapture` is constructed unconditionally; after tasks finish, captured LLM calls are flushed to `<state_dir>/evals/<task>/<run_iso>.jsonl`. `--analysis` reshapes the run into "expensive inspection mode" (reasoning on, ELI5 filter reasons, item/feed truncation, dry-run, render to stdout).
- `tasks.py` — task orchestration. Every LLM-touching stage takes a `collector=` (always-on `RunCapture`) and an `analysis: bool = False`. `analysis` controls dry-run, item/feed truncation, `reasoning=True` on adapter calls, and forcing `explain=True` on the filter prompt; the collector controls only what gets recorded.
  - `_pull_feeds(source, feed_cfgs, feeds_state, task_filter, session, *, collector, analysis)` — fetches all feeds concurrently via `RSSSource`, merges heuristic filters, returns `{url: PullResult | None}`. Under `analysis`, `seen` is empty so all items look fresh.
  - `_summarize_items(items, ..., *, collector, analysis)` — concurrently fetches item content and summarizes via LLM; sets `item.summary`. Passes `reasoning=analysis` through.
  - `_apply_curate(items, filter_cfg, ..., *, collector, analysis)` — groups items by source, calls `curate_entries` (with `reasoning=analysis` and `explain` forced when `analysis`), maps results back onto items as `filter_pass`/`filter_reason`, retries once on failure; returns `FilterResult`.
  - `_process_search_task` — LLM web-search pipeline: `SearchSource.pull()` → `DiscordTextTarget.push()` (+ `FileEmbedTarget` if configured). Skipped under `analysis` after recording.
  - `_process_feed_task` — RSS/digest pipeline: pull → summarize → curate → `DiscordEmbedTarget`, `DiscordMarkdownTarget`, or `DiscordDigestTarget` (+ file target if configured) → state update. Push step short-circuits under `analysis`.
  - `_process_scraper_task` — scraper pipeline: `ScraperSource.pull()` → `DiscordEmbedTarget` or `DiscordMarkdownTarget` → state update. Skipped entirely under `analysis`.
  - `_is_due` checks period with 60s grace. `_merge_filter` combines task-level and feed-level heuristic filter dicts.

#### `config/` — config loading and validation

- `config/__init__.py` — `load_config(path)` reads YAML or JSON. `validate_config(config)` uses Pydantic models to validate the full config and returns a list of error strings. The global LLM config is split into four top-level sections: `llm` (API keys only, under `llm.api_key`), `curate`, `search`, `summarize` (each can carry its own `model` spec); task/feed-level `curate.model` etc. override the matching global section. Model specs are single-key `{provider: model_name}` dicts (e.g. `{openai: gpt-4o-mini}`); `_resolve_model_spec` returns `(provider, model_name)` from one, and `resolve_model_specs` returns a list from either a single spec or a list (list = fallback chain, tried in order). The `_ModelSpec` / `_ModelSpecList` validators enforce single-key dicts with a known provider (`openai`, `gemini`, `deepseek`, `anthropic`). Also houses `parse_color`, `parse_period`, `task_kind` (returns the explicit `kind:` key if present; otherwise infers from `pull` list: `scraper` item → scraper, `search` item → search, else feeds), `get_api_key_for_provider`, and helpers: `get_feeds`, `get_discord_cfg`, `get_search_cfg`, `_get_scraper_cfg`, `get_file_path`.
- `config/config.yaml.template` — canonical reference for all supported config keys and defaults.

#### `state/` — state I/O and migrations

- `state/__init__.py` — `load_state`, `save_state` (writes `.old` backup, stamps `_version` and `_last_run`), `_auto_clean` (removes malformed items), and `_remove_unknown` (prunes tasks/feeds absent from config).
- `state/migrate.py` — state schema migrations. `needs_migration(state)` checks `state["_version"]` against `CURRENT_VERSION` (3). `migrate(state)` steps through `_STEPS` until current. The v2 migration nests all task keys under a top-level `"tasks"` key; the v3 migration renames `access_date` → `first_seen` on every item.
- `state/state.json.template` — example state file shape.

#### `process/` — between-pull-and-push processing stages

- `process/curate.py` — LLM-based feed entry classifier. Defines `FilterDecisions(items: list[FilterItem], memory: list[CurateParagraph])` Pydantic schema; `curate_entries(items, filter_cfg, ...)` classifies items grouped by source via `adapter.complete_structured(payload, FilterDecisions, ...)` — provider-native structured output (`instructor` in tool-calling mode) handles the JSON parse + schema validation + retry. Filter criteria come from `filter_cfg["criteria"]`. Returns `(results_dict, paragraphs) | None`. `results_dict` maps `str(id)` → `{"pass": bool, "reason": str}` (the Pydantic `FilterItem.passes` field serializes as JSON `"pass"` for LLM-friendliness); `paragraphs` is `list[MemoryParagraph]` (plain-text `text` + `citations: list[int]`), converted from `CurateParagraph` at the return boundary. `CurateParagraph` is the Pydantic model used for structured output; `MemoryParagraph` (NamedTuple from `pipeline.py`) is used everywhere else. When `explain: true`, passing-item reasons are ELI5-style (2–3 sentences). Feed-level bypass: feeds with `curate.skip: true` are tagged in `tasks.py` via `item.meta["curate_skip"]` and short-circuited to `filter_pass=True` before the LLM call (other feeds in the same task still get curated normally).
- `process/filter_heuristic.py` — pure regex-based filters used by `pull/feed.py` during feed parsing. `apply_regex(cfg, text)` runs `extract` / `replace` / `remove_phrases_with_urls` / `remove_phrases_containing` / `clear` ops; `url_filtered(url, cfg)` checks `skip_containing`.
- `process/summarize.py` — LLM summarization + article/transcript extraction. `summarize_entry(title, body, adapter, ...)` summarizes a feed entry. `summarize_transcript(transcript, adapter, ...)` summarizes a YouTube transcript. `fetch_item_content(url, session)` returns `(content, og_image)` — article text via trafilatura plus the article's `og:image` URL scraped from the same HTML; YouTube URLs are fetched via `yt-dlp` (subtitle VTTs downloaded with `writesubtitles` / `writeautomaticsub` flags, then parsed and SponsorBlock-filtered) and return the transcript with `og_image=None`. `extract_og_image(html)` is the standalone helper used at end of the trafilatura extraction.

#### `pull/` — source implementations

- `pull/feed.py` — feed fetching, dedup, and entry enrichment.
  - `RSSSource(Source)` — concrete source; wraps `get_new_entries`.
  - `get_new_entries(feed_cfg, seen, session)` — fetches and parses the feed, returns `(feed_title, current_items, new_entries: list[Item])` or `None` on parse failure. `feed_title` is the resolved display name (`cfg.name → parsed.feed.title → url`) and gets propagated through `PullResult.name` onto the feed dict in state. `current_items` dicts include `source_date` (ISO8601) when the entry has a pubDate/updated date. Entry ID is `entry.link`; entries with no link or older than 7 days are skipped. Bodies are HTML-stripped, truncated to 512 chars, and Markdown-escaped. Heuristic filters (`filter.title`, `filter.description`, `filter.url`) are applied via `process.filter_heuristic.apply_regex` and `url_filtered`. Supported ops: `extract` (regex), `replace`/`with`, `remove_phrases_with_urls`, `remove_phrases_containing`, `clear`, `skip_containing`.
- `pull/search.py` — LLM web-search source.
  - `SearchSource(Source)` — calls `run_search_task` and wraps the response as a single `Item`.
  - `run_search_task(task_cfg, instructions, model, adapter)` — calls the configured LLM with web search enabled, returns plain-text response or `None`.
- `pull/scraper.py` — browser-based extraction via Camoufox (hardened Firefox build with C++-level anti-detection patches). Drop-in Playwright API; the `SiteAdapter.scrape()` contract is unchanged.
  - `ScraperSource(Source)` — launches a headless Camoufox browser, delegates to a site adapter, returns new listings as `Item`s. Camoufox manages the fingerprint itself — adapters must not set a custom User-Agent.
- `pull/scrapers/base.py` — `SiteAdapter` ABC and the `@register_adapter` decorator-based registry (`get_adapter`, `available_adapters`).
- `pull/scrapers/vivareal.py` — `VivaRealAdapter`: parses property listings from VivaReal search pages.
- `pull/weather.py` — `WeatherSource(Source)`: fetches Open-Meteo forecast JSON (no auth), formats a wttr.in-style plain-text message into `Item.body`, returns a single `Item`. Helpers: `_format_message`, `_format_today` (header + daily summary + hourly rows at 6/9/12/15/18/21h), `_format_forecast` (one compact line per upcoming day), `_uv_window` (first contiguous hourly block ≥ threshold), `_wmo_emoji` (WMO code → emoji), `_uv_label` (PT-BR label by index value), `_build_url`. Uses `zoneinfo.ZoneInfo` (stdlib) for local-time alignment; no new dependencies.

#### `push/` — target implementations

- `push/discord.py` — Discord webhook targets and underlying posting functions.
  - `DiscordEmbedTarget(Target)` — posts each item as a Discord embed. The embed image is `Item.image` (set during pull from the feed entry or during summarize from the article's `og:image`); `item.meta["skip_image"]` (from task/feed `image.skip`) suppresses it.
  - `DiscordTextTarget(Target)` — posts each item's body as a plain text message (truncated to 2000 chars).
  - `DiscordMarkdownTarget(Target)` — posts each item as `### [title](<url>) source\nbody` (markdown format, no embed).
  - `DiscordDigestTarget(Target)` — renders each `MemoryParagraph` to plain text with source links appended (`[[Source](<url>)]`), then posts as ≤2000-char chunks.
  - All use `_post_webhook` which retries once on 429.
- `push/file.py` — file-based target implementations. Path is expanded (`~`, env vars) and parent dirs are created on first write.
  - `FileEmbedTarget(Target)` — appends each item as `## [Title](url)\n*source · date*\n\nbody\n\n---` blocks to the configured file.
  - `FileDigestTarget(Target)` — renders each `MemoryParagraph` to markdown with source links appended (`[Source](url)`), then appends `## YYYY-MM-DD\n\ndigest text\n\n---` to the configured file.

#### `providers/llm/` — LLM provider adapters

- `providers/llm/__init__.py` — `get_adapter(provider, api_key)` factory; returns `OpenAIAdapter`, `GeminiAdapter`, `AnthropicAdapter`, or `DeepSeekAdapter`. Also exports `FallbackAdapter` (tries entries in order until one returns non-None).
- `providers/llm/base.py` — `LLMAdapter` ABC with two abstract methods: `complete(...) -> LLMResponse | None` for free-form text and `complete_structured(prompt, response_model, ...) -> BaseModel | None` for provider-native structured output (via `instructor`, see curate docs below). Plus the `LLMResponse` dataclass (`text`, `model`, `input_tokens`, `output_tokens`, `latency_s`, `reasoning`, `finish_reason`). `complete()` accepts `web_search: bool | dict = False` and `reasoning: bool | dict = False`; truthy `reasoning` maps to the provider's thinking/extended-reasoning API and populates `LLMResponse.reasoning`.
- `providers/llm/openai.py` — OpenAI adapter (Responses API, supports `web_search_preview` and `reasoning={"effort": "high", "summary": "auto"}`).
- `providers/llm/gemini.py` — Gemini adapter (`google-genai`, supports Google Search tool and `ThinkingConfig(include_thoughts=True)`).
- `providers/llm/anthropic.py` — Anthropic adapter (streaming Messages API, supports `web_search` tool and `thinking={"type": "enabled", "budget_tokens": ...}`).
- `providers/llm/deepseek.py` — DeepSeek adapter (OpenAI-compatible Chat Completions; reasoning routes to `deepseek-reasoner` and captures `reasoning_content`).

#### `evals/` — captured LLM-call traces and replay

- `evals/capture.py` — `RunCapture` collects every LLM call's instructions, input, response, tokens, latency, optional reasoning. Exposes `to_jsonl_records()`, `write_jsonl()`, `to_json()`, and rich `display()`.
- `evals/replay.py` — reads a JSONL produced by `RunCapture` and re-issues each captured call against a list of `provider:model` pairs (only the model varies; instructions/input are used verbatim). Writes a side-by-side JSON report to `<state_dir>/evals/replays/`.

### State shape

State is keyed by task name under a top-level `"tasks"` key. Meta keys live at the top level:

```json
{
  "_version": 3,
  "_last_run": "<iso8601 utc>",
  "_last_clean": "<iso8601 utc>",
  "tasks": {
    "my-feeds": {
      "feeds": {
        "https://feed1.url": {
          "name": "Feed 1",
          "items": [
            {"url": "...", "title": "...", "source_date": "<iso8601 utc>", "first_seen": "<iso8601 utc>", "filter_pass": true, "filter_reason": "..."},
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

- `feeds` sub-dict holds per-URL state. Each entry has a `name` (resolved feed title from cfg → feed `<title>` → url), `items`, and `last_run`. Each successful fetch replaces `items` with the feed's current entries (bounded by feed length). `first_seen` is stamped when an item is first seen by claudinho and carried forward; `source_date` is the entry's original pubDate from the feed (when present). `filter_pass` and `filter_reason` are only present on items from tasks with a `curate` key.
- `memory` is only present on curated RSS/digest tasks. Each run appends one entry keyed by ISO8601 timestamp; history is capped at 20 entries (oldest evicted). The LLM receives the last 5 entries as context on each run.
- Search tasks store only `last_run` directly under the task name key.
- `load_state` returns the parsed JSON as-is; absent or `null` `last_run` always means "due now".
- `_last_clean` (top-level) is reserved as a meta key (recognized by `state/migrate.py`) and appears in the template, but no code path currently writes it.
- Use `--migrate` to update an old state file to the current schema. Use `--regenerate-state` to rebuild state from scratch.

### Config shape

See `config/config.yaml.template` — it is the canonical reference for all supported keys and their defaults.

Any change that adds, removes, or renames a config key must also update the corresponding Pydantic model in `config/__init__.py` so validation stays in sync.

### Tests (`tests/`)

Pipeline + helper test suite (21 tests, ~0.3s). Run with `uv run pytest`.

Tests call `_process_feed_task` and `_process_search_task` directly (not the `_async_main` orchestrator, not the CLI). Two boundaries are faked; everything else is real:
- **`FakeLLMAdapter`** (`tests/conftest.py`) — `LLMAdapter` subclass with two queues: `queue()` / `queue_text()` for `complete()` calls and `queue_structured()` / `queue_filter()` for `complete_structured()` calls. Passed as `curate_adapter=` and `summarize_adapter=` on `_process_feed_task`, or `search_adapter=` on `_process_search_task` — no monkeypatching needed. Exhausted queues raise loudly so missing canned responses fail the test rather than hanging.
- **`aioresponses`** — intercepts at the `aiohttp` transport layer. Real `RSSSource`, real `Discord*Target` (including `_post_webhook` retry-on-429), real `File*Target`, real `fetch_item_content` (article + `og:image` extraction via trafilatura) all run; only the network is mocked.

Fixtures live in `tests/fixtures/` (`feed_basic.xml`, `feed_b.xml`, `article_basic.html`). Helpers `make_curate_cfg` / `make_search_cfg` build task config dicts inline.

Scenarios covered:
- Curate happy path, already-seen dedup, single-feed pull failure (`None` from `Source.pull()` must not update `last_run`), filter-fails-twice fail-open.
- Digest with `cite_map` resolution (structured `MemoryParagraph.citations` IDs → `[Source](url)` in `FileDigestTarget` and `[[Source](<url>)]` in `DiscordDigestTarget`; stored memory contains only plain `text` fields, no markers to strip).
- Search task happy path.
- `tasks._merge_filter` (task-level vs feed-level filter merging across single/list shapes).
- `main._merge_task_results` orchestrator invariant — empty / exception task results leave state untouched.
- `main._prune_old_files` retention sweep.

Not covered: scraper task (Camoufox), `asyncio.gather(..., return_exceptions=True)` isolation at the `_async_main` level. The only `monkeypatch` in the suite is `asyncio.sleep` in the fail-open test (to skip the 10s curate retry delay) — stdlib, not a production class.

`get_new_entries` reverses feedparser's order (oldest-first), so the LLM curate filter sees XML items in reverse: the first item in the XML is `id=N-1`, the last is `id=0`. Keep this in mind when wiring `queue_filter` responses.

### Benchmark (`benchmark/`)

Standalone script that runs a fixed set of URLs through multiple LLM providers and compares their summaries.

- `benchmark/__main__.py` — entry point; run with `uv run benchmark/`
- `benchmark/config.yaml` — active config (not committed); copy from `benchmark/config.yaml.template`
- `benchmark/config.yaml.template` — documents `urls` (list of YouTube or article URLs) and `models` (list of `{provider, model, label}` dicts)
- `benchmark/results/` — JSON output files (`benchmark_<timestamp>.json`), one per run

Output JSON shape: `{timestamp, models: [{provider, model}], results: [{url, title, kind, fetch_error, summaries: [{provider, model, elapsed, summary, error}]}]}`

### Eval data

Every run leaves a per-task JSONL of LLM calls under `<state_dir>/evals/<task>/<run_iso>.jsonl`, one record per call. Record shape:

- Common keys: `task`, `call_type` (`filter` | `summarize` | `search`), `ts`, `model_used`, `instructions`, `response`, `input_tokens`, `output_tokens`, `latency_s`, `reasoning`, `web_search`.
- `filter` adds: `payload` (list of source groups with items, each item has `id`, `title`, `url`, optional `description`), `parsed` (per-item `id`, `source`, `title`, `url`, `pass`, `reason`), `memory`, `source_groups_count`, `items_count`, `passing_count`, `model` (configured spec).
- `summarize` adds: `input` (text sent to the LLM), `item_id`, `item_title`, `item_url`, `fetched_body`.
- `search` adds: `prompt`, `model` (configured spec).

Replay output at `<state_dir>/evals/replays/<source_basename>__replay_<ts>.json` has shape `{source_run, replayed_at, models, call_filter, calls: [{call_type, task, ts, web_search, item_id?, item_title?, results: [{model, text, input_tokens, output_tokens, latency_s, reasoning, is_original?, error?}, ...]}]}`. The first entry in `results` is the original captured response (`is_original: true`); subsequent entries are one per replayed `provider:model`.

Caveats:
- Replays of calls with `web_search: true` (always for `search`, configurable for `filter`) are noisy because each run gets different search results.
- Replay uses captured `instructions` + `input` verbatim — only the model varies. Prompt-change comparisons require capturing a new run after the change.
- `reasoning` is provider-dependent: only populated when the provider returns a reasoning trace (OpenAI o-series summary, Anthropic thinking blocks, Gemini thoughts, DeepSeek `reasoning_content`). Non-reasoning models return `reasoning: null` even under `--analysis`.

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-task exceptions, the gather uses `return_exceptions=True`.
- Keep the 2-second sleep between posts in the same task (Discord webhook rate limits).
- Don't add a sync HTTP path; feed fetching and article+og:image extraction during summarize all run concurrently via the shared aiohttp session.
- Only update `last_run` on a successful feed fetch. A `None` from `Source.pull()` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
- LLM curate failures retry once after 10s; on second failure all items are treated as passing (fail-open).
- All feeds in a given RSS/digest task share one LLM curate call; items are sent grouped by source with monotonically increasing integer IDs across all feeds.
- `Item.meta` carries per-item display hints (e.g. `color`) set during the pull stage so the target doesn't need to re-resolve them from config.
- `web_search` is plumbed through the curate call but intentionally not through `summarize_entry`: summaries run on already-fetched article content, so extra web search is wasted tokens.
- Feed-level `curate.skip: true` short-circuits the LLM curate call for that feed only — items always pass. Useful for trusted, low-volume feeds where the curate cost isn't justified. Other feeds in the same task still get curated normally.
- When changing architecture (new/renamed modules, classes, or functions), CLI flags, config keys, or state shape, update `CLAUDE.md` in the same commit. The existing rule about config-key changes updating the Pydantic model is the same idea, extended to docs.
