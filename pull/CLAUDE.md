# pull/

Source implementations. Each implements `pipeline.Source` and returns `PullResult` (or `None` on failure — caller must not update state).

## `feed.py` — RSS/Atom feeds

`RSSSource(Source)` wraps `get_new_entries(feed_cfg, seen, session)`, returning `(feed_title, current_items, new_entries: list[Item])` or `None` on parse failure.

A `youtube` pull item is **not** a separate source — `config.get_feeds` expands it into a `feed` dict (URL built from `channel_id`), so it flows through `RSSSource` unchanged. See `config/CLAUDE.md`.

- `feed_title` resolves `cfg.name → parsed.feed.title → url` and propagates through `PullResult.name` onto the feed dict in state.
- `current_items` dicts include `source_date` (ISO8601) when the entry has a pubDate/updated date.
- Entry ID is `entry.link`; entries with no link or older than 7 days are skipped.
- Bodies are HTML-stripped (BeautifulSoup `get_text`), truncated to 512 chars, Markdown-escaped.
- `_entry_image` resolves a single image URL from feed metadata only, via fallback chain: `media_thumbnail` → `media_content` (medium `image` or `image/*` type) → `enclosures` (`image/*`) → `links` (`rel=enclosure`, `image/*`). It does not scrape `<img>` tags out of body HTML.
- `get_new_entries` reads the **already-resolved** `ignore` / `skip` / `description` / `title` blocks off the feed cfg — `tasks/feeds.py:pull_feeds` merges the global→task→feed (+ youtube-scope) layers and injects them. The blocks:
  - `ignore.description` → drop the body entirely (`body = ""`); else `description` runs `apply_regex` (`remove`/`extract`/`replace`); `title` likewise. `ignore.image` is consumed later (`tasks/feeds.py` meta loop), not here.
  - `skip.url_contains` → `url_filtered`; `skip.shorts` → `/shorts/` URL check; `skip.livestreams` → livestream check.
- `skip.shorts` drops unseen entries whose link contains `/shorts/` (YouTube surfaces Shorts as `/shorts/<id>` links; regular videos are `/watch?v=`). Cheap URL check, no fetch — self-gates to YouTube.
- `skip.livestreams` drops livestreams (live, upcoming, or VODs of past streams). The feed carries no live flag, so `_is_livestream` fetches each new `/watch` page directly via the shared aiohttp session (browser UA) and checks `"isLiveContent":true` — vascod can't serve this since it routes YouTube URLs to its transcript adapter. Runs **last and only on survivors** of the url/shorts filters (`_live_url_set`, concurrent — non-`/watch` URLs are skipped, so it self-gates to YouTube), since it costs one fetch per entry. **Fail-open**: a fetch error or non-200 keeps the entry. One fetch per video ever (the entry is then marked seen).
- Dropped entries (url/shorts/livestreams) stay in `current_items` so they're marked seen and not reconsidered.

**Gotcha:** `get_new_entries` reverses feedparser's order (oldest-first). So when the LLM curate filter sees XML items, the first XML item is `id=N-1`, the last is `id=0`. Tests need to wire `queue_filter` responses accordingly.

## `research.py` — agentic research over vasco

`run_research_task(task_cfg, instructions, model, *, adapter, reasoning, trace)` runs a bounded agentic loop: each turn the LLM emits a `ResearchAction` (`search` / `read` / `finish`) via `complete_structured`; claudinho executes it against vascod (`_vasco.search` for a real DDG/Tavily SERP, `_vasco.extract` for query-ranked page passages), accumulates sources in `ResearchState`, then synthesizes a final cited answer via `complete`. Bounded by `max_steps` / `max_searches` / `max_reads` (termination guarantees, not cost tracking). Never raises — a vascod `None` is treated as "no results", an LLM `None` ends the loop early and we synthesize from what was gathered. Replaces the old provider-`web_search` search source; retrieval now comes from vasco (cache, escalation, quality scoring). `tasks/research.py:process_research_task` wraps it into the pull→push flow.

## `realestate.py` — structured real-estate listings via vasco

- `pull_realestate(realestate_cfgs, seen_per_url)` fetches each configured `url` concurrently via vasco's `realestate` adapter (`fetch_listings` in `process/_vasco.py`), which returns an envelope with normalized listing dicts in `quality.listings`. Returns `dict[url, PullResult | None]`; `None` means that source failed (missing url, fetch error, or vasco didn't route it to the realestate adapter) — caller must preserve its prior state.
- All parsing lives in vasco now: it picks the per-portal parser by domain (vivareal / portal-a / portal-b), handles transport (HTTP-first, browser escalation, SQLite cache, per-domain strategy), and emits normalized fields (`price`, `area`, `bedrooms`, `images`, …). claudinho only maps listings → `Item` (`_to_item`) and applies policy. `_to_item` absolutizes relative image URLs via `_abs_url` (`urljoin(listing["url"], img)`) — some portals (portal-a) emit `../imoveis/x.jpeg`, which Discord rejects as a non-absolute embed image URL (HTTP 400). Ideally vasco's parser would emit absolute URLs; this is claudinho's boundary-layer backstop.
- Policy helpers: `_passes_area_per_room` (drops cramped layouts when `min_area_per_room` is set; unknown area/bedrooms pass through), `_passes_neighborhood` (block-list when `exclude_neighborhoods` is set; drops listings matching an excluded entry via case/accent-insensitive substring match (`_normalize`); listings with no `neighborhood` field pass through), `_format_body`/`_title` (Discord rendering), `max_items` cap on new items per run.

## `weather/` — Open-Meteo forecast

`WeatherSource(Source)` (`weather/__init__.py`, with `build_url`) fetches forecast JSON (no auth), formats text into `Item.body`, returns a single `Item`. The URL passes `past_days=7` so each response includes the last 7 days of actuals — the smart-mode practical baseline rides on the same HTTP call as the forecast.

Package layout: `common.py` (shared helpers + `get_json`), `verbose.py` (default formatter), `smart.py` (signal-only formatter + anomaly machinery + its thresholds), `climate.py` (Archive-API normals).

Two formatter branches by `cfg["kind"]`:

- **Verbose (default)** — `verbose.format_message` → `_format_today` (header + daily summary + hourly rows at 5/7/9…23h) + `_format_forecast` (one compact line per upcoming day).
- **Smart (`kind: smart`)** — `smart.format_smart_message` → `_format_smart_today` (header + apparent min/max + conditional UV / rain / hot (`🥵 quente`) / cold (`🥶 frio`) interval lines + golden-hour line) + `_format_smart_forecast`. The four interval signals all run through one primitive, `common.threshold_windows` (full-24h scan, **all** contiguous blocks ≥ `min_hours`, each with its peak), rendered via `_join_windows` (`10h–16h, 19h–21h`). Hot/cold/UV use `min_hours=MIN_WINDOW_HOURS` (2); **rain uses `min_hours=1`** — a lone high-probability hour is worth flagging. UV uses the block peak for `(pico N)`; rain's daily prob/mm gate still decides whether the line appears at all, then the hourly blocks fill the window. Each upcoming forecast day fires only if rain crosses thresholds OR an apparent-temp / humidity anomaly fires.

### Anomaly detection

- `_evaluate_anomaly(value, hist, recent)` checks two frames with OR semantics: hist (climate normals — `SIGMA_HIST` σ) and recent (past 7 days — `SIGMA_RECENT` σ).
- The stronger frame renders via `_render_anomaly_suffix` as e.g. `(+5° vs normal 28°)` or `(+3° vs semana 30°)`.
- σ-multipliers drive the decision but are intentionally omitted from the rendered line to keep it scannable.
- If both baselines are unavailable, the anomaly section is silently skipped (rain may still emit).

### Baseline helpers

- `_baseline_from_normals(normals)` extracts `(μ, σ)` per metric from the cache.
- `_recent_baseline(daily, hourly, day_idx)` computes `(μ, σ)` over the 7 daily indices before today using `statistics.fmean` / `statistics.stdev`.
- Both return `dict[metric, (μ, σ) | None]` — `None` when fewer than `RECENT_MIN_SAMPLES` valid values or required cache keys are missing.

### Climate fetch

- `climate.fetch_climate_normals(cfg, session)` hits the Open-Meteo Archive (5-year ERA5 window for the current calendar month), stores both μ and σ for each metric.
- `climate.climate_cache_fresh(cache, now_local)` requires `cache["month"]` to match the current local month **and** `apparent_max_std` to be present — old σ-less caches are silently treated as stale.

### Shared helpers (`common.py`)

`threshold_windows` (the unified smart-mode interval scanner: all 24h blocks ≥ `min_hours`, each as `(start, end, peak)`), `uv_window` / `_hourly_window` (single-block, every-2h `_DISPLAY_HOURS` scan — kept for the **verbose** formatter only), `daily_humidity_mean`, `wmo_emoji`, `uv_label`, `weekday_pt`, `header_line`, `day_value`, `find_today_idx`, `find_hourly_index`, `get_json`. Smart-only: `_pick_apparent_anomaly` (renders the apparent_max or apparent_min with the largest combined σ-magnitude — `_decision_magnitude`).

### Threshold constants (`smart.py`)

Module-level, not config-exposed: `RAIN_TODAY_PROB_MIN`, `RAIN_TODAY_MM_MIN`, `RAIN_NEXT_PROB_MIN`, `RAIN_NEXT_MM_MIN`, `SIGMA_HIST` (3.0), `SIGMA_RECENT` (2.0), `SIGMA_FLOOR` (0.1, avoids divide-by-near-zero), `RECENT_MIN_SAMPLES` (4), `COMFORT_TEMP_MIN`/`COMFORT_TEMP_MAX` (hot/cold cutoffs), `MIN_WINDOW_HOURS` (2 — shared min-duration for every interval line), golden-hour knobs; `CLIMATE_NORMAL_YEARS` (5) lives in `climate.py`.

### State cache

`tasks/weather.py:process_weather_task` reads `state["tasks"][name]["climate"]` and passes it via `cfg["_climate_normals"]`. Stale cache triggers a fresh fetch that's included in the returned task state slice. Uses `zoneinfo.ZoneInfo` (stdlib); no new dependencies.

## `finance.py` — yfinance quotes

`FinanceSource(Source)`. yfinance is sync — calls run via `asyncio.to_thread`. Cfg sub-key picks the mode:

- **`report`** — `_fetch_quote_with_history` (1y daily history → last close, 5-trading-days-ago close, 52w `High.max` / `Low.min`) per ticker. Renders `### TICKER` H3 followed by a `$price | ±N.N% wk | 52w: $low _(−X%)_ – $high _(+Y%)_` line under a `## 📊 Report — <date>` H2. `$` is prefixed unconditionally (no per-ticker currency lookup); % deltas are italicized.
- **`monitor`** — `_fetch_quote_fast` (fast_info: last_price). Compares current price against `state["tasks"][name]["tickers"][<t>]["last_price"]` for delta alerts and `band_side` for crossing alerts. Returns zero or one batched `Item` rendered by `_format_monitor` in the same H2/H3 shape as report. Delta + level firing together collapse into one section joined by `|`. State threads bidirectionally through cfg: `_state_tickers` (input), `_new_state_tickers` (output mutated onto cfg, read by `tasks/finance.py:process_finance_task`).

### Market-hours gating

Every monitor rule passes through `_is_market_open(exchange, now_utc)` before the yfinance fetch.

- `_infer_exchange(ticker)` classifies by suffix: `.SA` → b3, `=X`/`.NYB` → fx, `X-USD` → crypto, else us_equity.
- Per-rule `exchange:` overrides inference.
- Schedules (`_SCHEDULES`) are wall-clock times in IANA timezones — `zoneinfo` handles DST automatically.
- FX uses Sun 17:00 ET → Fri 17:00 ET cycle. Crypto is always open.
- Closed-market tickers are skipped (no fetch, no alerts) but prior state preserved as next open-tick baseline.
- Holidays not modeled — half-day or holiday closures fetch and see no movement, which is acceptable.
- `_now_utc()` is a module-level indirection so tests can monkeypatch the wall clock.

User writes yfinance symbols verbatim (e.g. `DX-Y.NYB`, `USDBRL=X`) — no alias map. `_fetch_quote_with_history` and `_fetch_quote_fast` are module-level so tests monkeypatch them.
