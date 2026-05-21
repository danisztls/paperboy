# pull/

Source implementations. Each implements `pipeline.Source` and returns `PullResult` (or `None` on failure — caller must not update state).

## `feed.py` — RSS/Atom feeds

`RSSSource(Source)` wraps `get_new_entries(feed_cfg, seen, session)`, returning `(feed_title, current_items, new_entries: list[Item])` or `None` on parse failure.

- `feed_title` resolves `cfg.name → parsed.feed.title → url` and propagates through `PullResult.name` onto the feed dict in state.
- `current_items` dicts include `source_date` (ISO8601) when the entry has a pubDate/updated date.
- Entry ID is `entry.link`; entries with no link or older than 7 days are skipped.
- Bodies are HTML-stripped, truncated to 512 chars, Markdown-escaped.
- Heuristic filters (`filter.title`, `filter.description`, `filter.url`) applied via `process.filter_heuristic.apply_regex` and `url_filtered`.
- Supported ops: `extract`, `replace`/`with`, `remove_phrases_with_urls`, `remove_phrases_containing`, `clear`, `skip_containing`.

**Gotcha:** `get_new_entries` reverses feedparser's order (oldest-first). So when the LLM curate filter sees XML items, the first XML item is `id=N-1`, the last is `id=0`. Tests need to wire `queue_filter` responses accordingly.

## `search.py` — LLM web-search source

`SearchSource(Source)` calls `run_search_task(task_cfg, instructions, model, adapter)`, which invokes the configured LLM with web search enabled and wraps the response as a single `Item`. Returns `None` on LLM error.

## `scraper.py` + `scrapers/` — browser-based extraction (Camoufox)

Camoufox is a hardened Firefox build with C++-level anti-detection patches. Drop-in Playwright API; the `SiteAdapter.scrape()` contract is unchanged.

- `pull_scrapers(scraper_cfgs, seen_per_adapter)` (module-level coroutine) launches one Camoufox browser, opens a fresh page per adapter (serial), runs each adapter's `scrape()`, returns `dict[adapter_name, PullResult | None]`. `None` means that adapter failed (unknown name, missing url, navigation/extraction error or exception) — caller must preserve its prior state.
- Camoufox manages the fingerprint itself — adapters must not set a custom User-Agent.
- `_get_scraper_cfgs(task_cfg)` returns the list of `scraper:` items from `pull:`.

### Adapters

- `scrapers/base.py` — `SiteAdapter` ABC + `@register_adapter` decorator registry (`get_adapter`, `available_adapters`).
- `scrapers/vivareal.py` — `VivaRealAdapter`: parses VivaReal search pages. Multi-image: both JSON-LD `image` and `__NEXT_DATA__` `medias` already expose the full gallery on the search page; capped at 4 for Discord's embed-merge limit.
- `scrapers/portal_a.py` — server-rendered PHP (no JS gating, no JSON-LD); reads `.pgl-property` cards via `page.evaluate()`. Price IS on the card. Multi-image: card only carries one `_360.jpeg` thumbnail; the adapter visits each listing's detail page (`imovel.php?id=N`) for new URLs (not in `seen`) and pulls up to 4 `_848.jpeg` gallery photos. Falls back to the card thumbnail on detail-page failure.
- `scrapers/portal_b.py` — WordPress + Elementor "Loop Grid". Reads `.imovel.type-imovel` cards. Specs from the first icon-list widget (positional: beds/baths/parking/area); location from the second. Type derived from `tipo_de_imovel-*` body classes (more stable than parsing labels). No price on the listing card. Single-image only: the detail page's gallery sits next to unrelated `wp-content/uploads/` images (other listings' photos, marketing assets) that can't be reliably separated from the listing's own gallery, so the adapter only surfaces the card thumbnail.

## `weather.py` — Open-Meteo forecast

`WeatherSource(Source)` fetches forecast JSON (no auth), formats text into `Item.body`, returns a single `Item`. The URL passes `past_days=7` so each response includes the last 7 days of actuals — the smart-mode practical baseline rides on the same HTTP call as the forecast.

Two formatter branches by `cfg["kind"]`:

- **Verbose (default)** — `_format_message` → `_format_today` (header + daily summary + hourly rows at 5/7/9…23h) + `_format_forecast` (one compact line per upcoming day).
- **Smart (`kind: smart`)** — `_format_smart_message` → `_format_smart_today` (header + apparent min/max + conditional UV window + conditional rain window + conditional comfort windows via `_comfort_windows`) + `_format_smart_forecast`. Each upcoming day fires only if rain crosses thresholds OR an apparent-temp / humidity anomaly fires.

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

- `fetch_climate_normals(cfg, session)` hits the Open-Meteo Archive (5-year ERA5 window for the current calendar month), stores both μ and σ for each metric.
- `_climate_cache_fresh(cache, now_local)` requires `cache["month"]` to match the current local month **and** `apparent_max_std` to be present — old σ-less caches are silently treated as stale.

### Shared helpers

`_uv_window`, `_rain_window` (scans all 24h for tighter resolution), `_daily_humidity_mean`, `_wmo_emoji`, `_uv_label`, `_pick_apparent_anomaly` (renders the apparent_max or apparent_min with the largest combined σ-magnitude — `_decision_magnitude`), `_build_url`.

### Threshold constants

Module-level, not config-exposed: `RAIN_TODAY_PROB_MIN`, `RAIN_TODAY_MM_MIN`, `RAIN_NEXT_PROB_MIN`, `RAIN_NEXT_MM_MIN`, `SIGMA_HIST` (2.0), `SIGMA_RECENT` (1.0), `SIGMA_FLOOR` (0.1, avoids divide-by-near-zero), `RECENT_MIN_SAMPLES` (4), `CLIMATE_NORMAL_YEARS` (5).

### State cache

`tasks.py:_process_weather_task` reads `state["tasks"][name]["climate"]` and passes it via `cfg["_climate_normals"]`. Stale cache triggers a fresh fetch that's included in the returned task state slice. Uses `zoneinfo.ZoneInfo` (stdlib); no new dependencies.

## `finance.py` — yfinance quotes

`FinanceSource(Source)`. yfinance is sync — calls run via `asyncio.to_thread`. Cfg sub-key picks the mode:

- **`report`** — `_fetch_quote_with_history` (1y daily history → last close, 5-trading-days-ago close, 52w `High.max` / `Low.min`) per ticker. Renders `### TICKER` H3 followed by a `$price | ±N.N% wk | 52w: $low _(−X%)_ – $high _(+Y%)_` line under a `## 📊 Report — <date>` H2. `$` is prefixed unconditionally (no per-ticker currency lookup); % deltas are italicized.
- **`monitor`** — `_fetch_quote_fast` (fast_info: last_price). Compares current price against `state["tasks"][name]["tickers"][<t>]["last_price"]` for delta alerts and `band_side` for crossing alerts. Returns zero or one batched `Item` rendered by `_format_monitor` in the same H2/H3 shape as report. Delta + level firing together collapse into one section joined by `|`. State threads bidirectionally through cfg: `_state_tickers` (input), `_new_state_tickers` (output mutated onto cfg, read by `tasks.py:_process_finance_task`).

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
