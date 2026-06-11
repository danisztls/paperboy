# process/

Between-pull-and-push processing stages.

## `curate.py` — LLM-based feed entry classifier

- `curate_items(all_items, curate_cfg, handle, ...)` — the full curate stage over `Item`s: builds the grouped payload (by source, monotonically increasing int IDs), calls `curate_entries`, retries once after 10s, decodes verdicts back onto the items (`filter_pass`/`filter_reason` via `dataclasses.replace`), and returns a `pipeline.CurateResult` (annotated items + memory paragraphs + cite_map). **Fail-open**: a second LLM failure returns all items as passing. Takes a `providers.llm.ModelHandle` and the always-on collector.
- `curate_entries(items, curate_cfg, ...)` classifies the already-built payload via `adapter.complete_structured(payload, FilterDecisions, ...)` — each adapter calls the provider's native structured-output API and validates the returned JSON against the model.
- `FilterDecisions(items: list[FilterItem], memory: list[CurateParagraph])` Pydantic schema for structured output.
- Filter criteria come from `curate_cfg["criteria"]`.
- `curate_entries` returns `(results_dict, paragraphs) | None`:
  - `results_dict` maps `str(id) → {"pass": bool, "reason": str}` (the Pydantic `FilterItem.passes` field serializes as JSON `"pass"` for LLM-friendliness).
  - `paragraphs` is `list[MemoryParagraph]` (plain-text `text` + `citations: list[int]`), converted from `CurateParagraph` at the return boundary.
- `CurateParagraph` is the Pydantic model used for structured output; `MemoryParagraph` (NamedTuple from `pipeline.py`) is used everywhere else.
- When `explain: true`, passing-item reasons are ELI5-style (2–3 sentences); `analysis` forces it.
- Feed-level bypass: feeds with `curate.skip: true` are tagged in `tasks/feeds.py` via `item.meta["curate_skip"]` and skipped from the LLM payload (they always pass).

## `filter_heuristic.py` — regex-based filters

Pure functions used by `pull/feed.py` during feed parsing:

- `apply_regex(cfg, text)` runs a flat `_TextTransform` op-set on a single field: `remove` (raw regex or list, `re.sub`'d out), `extract` (keep the match), `replace`/`with`. Consumed for the `description`/`title` blocks. Dropping a whole field (`ignore.description`) is handled in `pull/feed.py`, not here.
- `url_filtered(url, needles)` checks whether the URL contains any of `skip.url_contains`.

## `_vasco.py` — thin client to the vascod daemon for URL content extraction

claudinho **no longer imports vasco as a library** — it is a thin client of `vascod`, the resident vasco daemon (`vasco serve`), reached over a UNIX socket (`$XDG_RUNTIME_DIR/vasco/vascod.sock`, overridable via `VASCO_SERVICE_SOCKET`). This module vendors a ~40-line stdlib socket client (length-prefixed JSON, mirroring `vasco/service/protocol.py`); `PROTOCOL_VERSION` is vendored and a mismatch is logged. The public function signatures are unchanged — only the transport moved.

- `configure()` — **no-op** retained for call-site compatibility (Config + Cache now live in vascod, not in claudinho's process).
- `fetch_content(url, refresh=False)` → `(markdown, og_image) | None` — sends `{op:"fetch"}`; used by `summarize_items` during batch summarization.
- `fetch_content_with_title(url, refresh=False)` → `(markdown, title, og_image, is_youtube) | None` — used by CLI `--summarize`, `--get-content`, and benchmark.
- `fetch_raw_html(url, mode="auto")` → `str | None` — sends `raw=True`; returns the original HTML.
- `fetch_listings(url, refresh=False)` → full envelope `| None` — real-estate listings (gated on `mode_used == "realestate"`); listings in `env["quality"]["listings"]`.

The daemon runs the same `mode="auto"` pipeline (HTTP-first, escalating to the shared Camoufox browser server on bot-blocked pages) over the one shared SQLite cache (`~/.cache/vasco/cache.db`), and adds cross-consumer single-flight + per-domain rate-limiting. CLI one-shots use `refresh=True`. **Requires vascod running** (`systemctl --user status vascod.service`); if it's unreachable, every fetch returns `None` (logged) and claudinho skips that item this run. Concurrency stays claudinho-side: `process/summarize.py`/`pull/realestate.py` `asyncio.gather` over many `fetch_content`/`fetch_listings` calls, which become N coordinated `fetch` messages to vascod (claudinho does not use a batch `fetch_many`).

## `summarize.py` — LLM summarization

- `summarize_items(items, cfg_by_id, handle, ...)` — the batch stage over `Item`s: fetches article content concurrently via `_vasco.fetch_content` (falling back to the feed body), calls `summarize_entry` per item, dedups by URL, fills `Item.image` from the article's og:image when the item had none, and records each call to the collector. `cfg_by_id` maps item id → (language, instructions).
- `summarize_entry(title, body, adapter, ...)` summarizes a feed entry.
- `summarize_transcript(title, transcript, adapter, ...)` summarizes a YouTube transcript.
- `run_summarize(url, adapter, model, language)` — CLI `--summarize` entry point. Fetches via `_vasco`, routes YouTube vs article, prints summary.
- `run_get_content(url)` — CLI `--get-content` entry point. Fetches via `_vasco`, prints extracted text.
