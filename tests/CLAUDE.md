# tests/

Pipeline + helper test suite (~21 tests, ~0.3s). Run with `uv run pytest`.

## Approach

Tests call `process_feed_task` / `process_research_task` / `process_weather_task` / `process_finance_task` directly (not the `_async_main` orchestrator, not the CLI), passing a `RunContext` built by `conftest.make_ctx(session, ...)`. Two boundaries are faked; everything else is real.

- **`FakeLLMAdapter`** (`conftest.py`) — `LLMAdapter` subclass with two queues: `queue()` / `queue_text()` for `complete()` calls and `queue_structured()` / `queue_filter()` for `complete_structured()` calls. Wrapped into `ModelHandle`s by `make_ctx(session, curate=..., summarize=..., research=...)` — no monkeypatching needed. Exhausted queues raise loudly so missing canned responses fail the test rather than hanging. The research loop additionally fakes `process._vasco.search`/`extract` (see `test_research.py`'s `FakeVasco`/`patch_vasco`).
- **`aioresponses`** — intercepts at the `aiohttp` transport layer. Real `RSSSource`, real `Discord*Target` (including `_post_webhook` retry-on-429), real `File*Target` all run; only the network is mocked. Content extraction runs through vasco (httpx-based, not intercepted by aioresponses).

Fixtures live in `tests/fixtures/` (`feed_basic.xml`, `feed_b.xml`, `article_basic.html`). Helpers `make_curate_cfg` / `make_research_cfg` build task config dicts inline.

## Scenarios covered

- Curate happy path, already-seen dedup, single-feed pull failure (`None` from `Source.pull()` must not update `last_run`), filter-fails-twice fail-open.
- Digest with `cite_map` resolution (structured `MemoryParagraph.citations` IDs → `[Source](url)` in `FileDigestTarget` and `[[Source](<url>)]` in `DiscordDigestTarget`; stored memory contains only plain `text` fields, no markers to strip).
- Research task happy path + loop limits (`max_steps`/`max_reads`), query/URL dedup, vascod-failure and decision-`None` resilience.
- Agentic corroboration curate (`curate.corroborate`): the search → finish → verdict loop over a warm conversation, with `process._vasco.search` faked; verdicts decode onto items and the final call carries a multi-turn `messages` conversation.
- `--analysis` filter render (`test_render.py`): the corroboration trajectory (searched queries + top hits) and cache hit ratio show on the agentic path, and are omitted on the standard path.
- `config.scope.layer_dict` (global→task→feed block merging: precedence, partial overrides, non-dict/None blocks skipped, inputs not mutated).
- `main.merge_task_results` orchestrator invariant — empty / exception task results leave state untouched.
- `main.prune_old_files` retention sweep.

## Not covered

- Real-estate task (vasco realestate adapter).
- `asyncio.gather(..., return_exceptions=True)` isolation at the `_async_main` level.

The only `monkeypatch` in the suite is `asyncio.sleep` in the fail-open test (to skip the 10s curate retry delay) — stdlib, not a production class.

## Gotcha

`get_new_entries` reverses feedparser's order (oldest-first), so the LLM curate filter sees XML items in reverse: the first item in the XML is `id=N-1`, the last is `id=0`. Keep this in mind when wiring `queue_filter` responses.
