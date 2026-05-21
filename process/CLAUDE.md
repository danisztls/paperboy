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

## `summarize.py` — LLM summarization + content extraction

- `summarize_entry(title, body, adapter, ...)` summarizes a feed entry.
- `summarize_transcript(transcript, adapter, ...)` summarizes a YouTube transcript.
- `fetch_item_content(url, session)` returns `(content, og_image)`:
  - Article text via trafilatura plus `og:image` scraped from the same HTML.
  - YouTube URLs fetched via `yt-dlp` (subtitle VTTs downloaded with `writesubtitles` / `writeautomaticsub` flags, parsed and SponsorBlock-filtered) return the transcript with `og_image=None`.
- `extract_og_image(html)` is the standalone helper used at end of trafilatura extraction.
- `configure_youtube_cookies(browser)` — set at startup from `youtube.cookies_from_browser` config. When set, every `yt-dlp` call adds `cookiesfrombrowser=(browser,)`, which is required to bypass the "Sign in to confirm you're not a bot" wall. Module-level state so deep call sites don't need to thread it.
