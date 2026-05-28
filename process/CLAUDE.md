# process/

Between-pull-and-push processing stages.

## `curate.py` — LLM-based feed entry classifier

- `FilterDecisions(items: list[FilterItem], memory: list[CurateParagraph])` Pydantic schema for structured output.
- `curate_entries(items, filter_cfg, ...)` classifies items grouped by source via `adapter.complete_structured(payload, FilterDecisions, ...)` — each adapter calls the provider's native structured-output API and validates the returned JSON against the model.
- Filter criteria come from `filter_cfg["criteria"]`.
- Returns `(results_dict, paragraphs) | None`:
  - `results_dict` maps `str(id) → {"pass": bool, "reason": str}` (the Pydantic `FilterItem.passes` field serializes as JSON `"pass"` for LLM-friendliness).
  - `paragraphs` is `list[MemoryParagraph]` (plain-text `text` + `citations: list[int]`), converted from `CurateParagraph` at the return boundary.
- `CurateParagraph` is the Pydantic model used for structured output; `MemoryParagraph` (NamedTuple from `pipeline.py`) is used everywhere else.
- When `explain: true`, passing-item reasons are ELI5-style (2–3 sentences).
- Feed-level bypass: feeds with `curate.skip: true` are tagged in `tasks.py` via `item.meta["curate_skip"]` and short-circuited to `filter_pass=True` before the LLM call.

## `filter_heuristic.py` — regex-based filters

Pure functions used by `pull/feed.py` during feed parsing:

- `apply_regex(cfg, text)` runs `extract` / `replace` / `remove_phrases_with_urls` / `remove_phrases_containing` / `clear` ops.
- `url_filtered(url, cfg)` checks `skip_containing`.

## `_vasco.py` — bridge to vasco for URL content extraction

Thin wrapper around the `vasco` library (sibling project) for content fetching and raw HTML retrieval.

- `configure()` — loads `vasco.config.Config` (from `~/.config/vasco/config.yaml`) + `vasco.cache.Cache` singletons. Called once at startup from `main.py`.
- `fetch_content(url, refresh=False)` → `(markdown, og_image) | None` — single-URL fetch via `vasco.fetch.fetch_one(mode="auto")`. Used by `tasks.py` during batch summarization.
- `fetch_content_with_title(url, refresh=False)` → `(markdown, title, og_image, is_youtube) | None` — single-URL fetch returning metadata. Used by CLI `--summarize`, `--get-content`, and benchmark.
- `fetch_raw_html(url)` → `str | None` — single-URL fetch with `raw=True`, returns the original HTML. Used by scraper adapters for detail page gallery fetching.

Vasco's `mode="auto"` tries plain HTTP first, escalating to browser (Camoufox) only on bot-blocked pages. Per-domain strategy learning means subsequent fetches to the same domain skip tiers that previously failed. Shares vasco's global SQLite cache (`~/.cache/vasco/cache.db`). CLI one-shots use `refresh=True` to bypass cache reads.

## `summarize.py` — LLM summarization

- `summarize_entry(title, body, adapter, ...)` summarizes a feed entry.
- `summarize_transcript(title, transcript, adapter, ...)` summarizes a YouTube transcript.
- `run_summarize(url, adapter, model, language)` — CLI `--summarize` entry point. Fetches via `_vasco`, routes YouTube vs article, prints summary.
- `run_get_content(url)` — CLI `--get-content` entry point. Fetches via `_vasco`, prints extracted text.
