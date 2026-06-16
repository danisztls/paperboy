# tasks/

Task orchestration: one `process_*_task(task_cfg, state, ctx)` coroutine per task kind. Each pulls from its source(s), pushes to the configured targets, and returns a `{task_name: task_state}` slice for `main.merge_task_results` — or `{}` when nothing should be persisted (failed pull/post, analysis dry-run), so the task retries on the next cron sweep.

## `context.py` — `RunContext` / `LLMHandles`

`RunContext` bundles everything that used to be threaded as individual kwargs: the shared `aiohttp.ClientSession`, the full global config dict, `LLMHandles` (the three globally-configured `ModelHandle`s: `curate`, `summarize`, `research`), the always-on `RunCapture` collector, and the `analysis` flag. Built once in `main._async_main`; tests build it via `tests/conftest.make_ctx(session, ...)` with defaults.

Conveniences: derived properties `language` (global `curate.language` or `EN-US`), `max_age_seconds` (global `feeds.max_age_days`), `research_instructions`; `record_push(n)` (collector-guarded); `capture_task(name, kind)` context manager bracketing a task for the collector (replaces the begin/finally-finish boilerplate).

`analysis` controls dry-run, item/feed truncation, `reasoning=True` on adapter calls, and forcing `explain=True` on the curate prompt; the collector controls only what gets recorded.

## `due.py` — period due checks

- `DEFAULT_PERIOD` (1h), `PERIOD_GRACE` (60s).
- `is_due(feed_state, period, now)` — sliding-window for `m`/`h`, calendar-aligned (local date / ISO week) for `d`/`w`.
- `task_is_due(task_cfg, task_state, period, now)` — task-level `last_run` for research/realestate/weather/finance; "any feed due" for feed tasks.

## `__init__.py` — public surface

Re-exports the processors plus `processor_for(kind)` (the kind→processor dispatch table used by `main._collect_due_tasks`; unknown kinds fall through to `process_feed_task`).

## Per-kind processors

- `feeds.py` — `process_feed_task`: pull all feeds concurrently (`pull_feeds`, which resolves the scoped `ignore`/`skip`/`description`/`title` blocks via `config.scope.resolve_scoped` and injects them into the feed cfg), tag items with display meta (`color`, `skip_image`, `curate_skip`), then `process.summarize_items` → `process.curate_items` → push (digest / embed / markdown target by kind + `discord.format`) → `feed_state.build_feed_task_state`. Also `regenerate_feeds_state` (the `--regenerate-state` mode).
- `feed_state.py` — `merge_feed_state` (per-feed item merge: stamp `first_seen`, carry summary/filter annotations, drop failed posts) and `build_feed_task_state` (all feeds + the coverage ledger). `apply_coverage(prev_ledger, coverage, now_iso)` upserts this run's topic updates: each `CoverageUpdate` continues an existing topic (id matched via `continues`, or a slug collision) — bumping `frequency`, refreshing `state`/`last_seen` — or seeds a new one; topics dormant past `LEDGER_ACTIVE_DAYS`=21 are evicted and the ledger is capped at `LEDGER_MAX_TOPICS`=60. Code owns `frequency`/timestamps so the trajectory bar reads a real count.
- `research.py` / `weather.py` / `finance.py` — thin wrappers around their `pull/` sources; all three post via `delivery.deliver_text`. Weather owns the smart-mode climate cache read/refresh; finance threads monitor state through the cfg (`_state_tickers` in, `_new_state_tickers` out) and persists state even on zero alerts so baselines advance.
- `realestate.py` — `process_realestate_task`: per-url state (`realestate[<url>]: {items, last_run}`), `__legacy__` dedup bucket, batched embed/markdown push. Not captured by the collector and skipped in analysis mode.
- `delivery.py` — `deliver_text(ctx, task_cfg, items, name)`: Discord text post + optional file target; returns False on post failure (caller must not save state).
