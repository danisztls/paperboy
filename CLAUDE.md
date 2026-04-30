# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal RSS → Discord webhook notifier. Polls a list of feeds, posts new entries as Discord embeds, persists which entry IDs have already been seen so the next run only sends what's new. Intended to be run on a cron, not as a long-lived process.

## Commands

The project uses `uv` (see `uv.lock`, `.python-version` pinning Python 3.14).

- Run: `uv run claudinho config.yaml`
- Debug a single entry without touching state: `uv run claudinho config.yaml --debug`
- Upgrade `state.json` to the current schema without touching feeds: `uv run claudinho config.yaml --migrate`
- Sync deps: `uv sync`

There is no test suite, linter, or formatter configured.

`config.yaml` is gitignored — copy `config.yaml.template` and fill in webhook URLs and feed URLs. `state.json` is also gitignored and is created next to the config on first non-debug run.

## Architecture

Three modules, no package, flat layout:

- `main.py` — CLI entry point and orchestration. Loads config + state, opens a single shared `aiohttp.ClientSession`, then dispatches to one of three modes:
  - **Normal mode**: filters out feeds whose `last_run` is more recent than the hook's `period` (with a 60-second grace window for cron drift), then builds one task per remaining `(webhook, feed)` pair and `asyncio.gather`s them in parallel; merges per-feed results into `state` and saves once at the end.
  - **Debug mode (`--debug`)**: walks feeds sequentially, posts the *first* new entry from the *first* feed that has one, then exits. Period is ignored. **State is never saved in debug mode** — this is deliberate, so the same entry can be re-posted while iterating on formatting.
  - **Migrate mode (`--migrate`)**: loads `state.json`, runs the legacy-shape migration in `load_state`, writes the result, and exits. Does not open an HTTP session, does not fetch feeds, does not post. The config file does not need to exist — only its parent directory matters (for the default `state.json` location). Mutually exclusive with `--debug`.
- `feed.py` — feed fetching, dedup, and entry enrichment. `get_new_entries(feed_cfg, seen, session)` returns `(current_ids, new_entries)` on success or `None` on parse failure (so the caller can skip the `last_run` update and retry on the next cron tick):
  - `current_ids` is *all* IDs currently in the feed (used to overwrite state — old IDs that fall off the feed are forgotten, which keeps `state.json` from growing forever).
  - Entry ID resolution falls back: `entry.id` → `entry.link` → `entry.title`.
  - Unseen entries are reversed into chronological order, then OG images are fetched concurrently (only the first 32KB of each article page is read — the parser stops at `<body>`).
  - Descriptions are HTML-stripped, truncated to 300 chars, and Markdown-escaped (`_MD_ESCAPE_RE` covers `*_` `~` `` ` `` and leading `>`/`#` per line) so feed content can't accidentally format Discord messages.
- `discord.py` — single-purpose: build an embed from a `FeedEntry` and POST it. Raises on HTTP ≥400 so `main.py` can log+skip that one entry without aborting the rest.

### State shape

`state.json` is `{feed_url: {"ids": [entry_id, ...], "last_run": "<iso8601 utc>" | null}}`. Each successful run replaces `ids` with the IDs currently in the feed (not a union, so the file size is bounded by feed length) and stamps `last_run` with `datetime.now(timezone.utc).isoformat()`. A failed fetch (network or parse error) leaves the entry untouched.

`load_state` transparently migrates the legacy `{feed_url: [entry_id, ...]}` shape: any list value is wrapped as `{"ids": <list>, "last_run": null}`, so a `null` `last_run` always means "due now".

### Config shape

```yaml
hooks:
  - webhook: "https://discord.com/api/webhooks/.../..."
    period: 1  # optional, hours between processing this hook's feeds. Default: 1.0
    feeds:
      - name: "Optional display name (used as embed footer)"
        url: "https://example.com/feed.xml"
```

Multiple `hooks` entries let different feed groups go to different webhooks. `period` is per-hook only — there's no per-feed override. The threshold check subtracts `PERIOD_GRACE` (60s) so a 1h cron firing every ~60min doesn't skip every other tick due to clock jitter. Both YAML and JSON are accepted (dispatched by file suffix in `load_config`).

## Conventions worth preserving

- Errors posting one entry must not kill the run — `main.py` catches per-entry, the gather uses `return_exceptions=True` per-feed.
- Keep the 1-second sleep between posts in the same feed (Discord webhook rate limits).
- Don't add a sync HTTP path; the OG-image fetch and feed posting are deliberately concurrent via the shared session.
- Only update `last_run` on a successful feed fetch. A `None` from `get_new_entries` must short-circuit the state write so a transiently broken feed retries on the next cron tick rather than waiting `period` hours.
