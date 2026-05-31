# tests/

Pipeline + helper test suite (~21 tests, ~0.3s). Run with `uv run pytest`.

## Approach

Tests call `_process_feed_task` and `_process_search_task` directly (not the `_async_main` orchestrator, not the CLI). Two boundaries are faked; everything else is real.

- **`FakeLLMAdapter`** (`conftest.py`) — `LLMAdapter` subclass with two queues: `queue()` / `queue_text()` for `complete()` calls and `queue_structured()` / `queue_filter()` for `complete_structured()` calls. Passed as `curate_adapter=` and `summarize_adapter=` on `_process_feed_task`, or `search_adapter=` on `_process_search_task` — no monkeypatching needed. Exhausted queues raise loudly so missing canned responses fail the test rather than hanging.
- **`aioresponses`** — intercepts at the `aiohttp` transport layer. Real `RSSSource`, real `Discord*Target` (including `_post_webhook` retry-on-429), real `File*Target` all run; only the network is mocked. Content extraction runs through vasco (httpx-based, not intercepted by aioresponses).

Fixtures live in `tests/fixtures/` (`feed_basic.xml`, `feed_b.xml`, `article_basic.html`). Helpers `make_curate_cfg` / `make_search_cfg` build task config dicts inline.

## Scenarios covered

- Curate happy path, already-seen dedup, single-feed pull failure (`None` from `Source.pull()` must not update `last_run`), filter-fails-twice fail-open.
- Digest with `cite_map` resolution (structured `MemoryParagraph.citations` IDs → `[Source](url)` in `FileDigestTarget` and `[[Source](<url>)]` in `DiscordDigestTarget`; stored memory contains only plain `text` fields, no markers to strip).
- Search task happy path.
- `tasks._merge_filter` (task-level vs feed-level filter merging across single/list shapes).
- `main._merge_task_results` orchestrator invariant — empty / exception task results leave state untouched.
- `main._prune_old_files` retention sweep.

## Not covered

- Real-estate task (vasco realestate adapter).
- `asyncio.gather(..., return_exceptions=True)` isolation at the `_async_main` level.

The only `monkeypatch` in the suite is `asyncio.sleep` in the fail-open test (to skip the 10s curate retry delay) — stdlib, not a production class.

## Gotcha

`get_new_entries` reverses feedparser's order (oldest-first), so the LLM curate filter sees XML items in reverse: the first item in the XML is `id=N-1`, the last is `id=0`. Keep this in mind when wiring `queue_filter` responses.
