# config/

Config loading, primitives/accessors, and validation. `__init__.py` only re-exports; the code lives in three modules:

## `loader.py`

- `load_config(path)` — reads YAML or JSON; YAML supports `!secret <key>` resolved against a sibling `secrets.yaml`.

## `values.py` — primitives and raw-dict accessors

- `Period` dataclass + `parse_period`, `parse_color`.
- `task_kind` (returns explicit `kind:` key if present; otherwise infers from the `pull` list: `realestate`/`research`/`weather`/`finance` item → that kind, else feeds).
- Accessors over raw config dicts: `get_feeds` (expands `youtube` sugar), `get_realestate_cfgs`, `get_discord_cfg`, `get_research_cfg`, `get_weather_cfg`, `get_finance_cfg`, `get_file_path`, `get_api_key_for_provider`.
- `is_youtube_feed_url` (URL-prefix test gating the `youtube:` scope).

## `schema.py` — Pydantic validation

- `validate_config(config)` — validates the full config against `_Config`, returns a list of error strings.
- `period` is a task-level key (`_Task.period`) **and** an optional per-feed override on `_PullFeedItem`/`_PullYouTubeItem` (same `_Period` grammar). `_Task._check_task` rejects per-feed `period:` on `kind: digest` tasks.
- Global LLM config is split into four top-level sections: `llm` (API keys only, under `llm.api_key`) and `curate` / `research` / `summarize` (each can carry its own `model` spec). Task/feed-level `curate.model` etc. override the matching global section.
- Model specs are verbose dicts: `{provider, name, reasoning?}` where provider ∈ `{deepseek, gemini, claude_cli}` and reasoning ∈ `{off, low, medium, high}` (absent = off). `claude_cli` shells out to the local `claude` CLI (Claude Code) and reuses its login by default — set `llm.api_key.claude_cli` only to opt into direct API billing.
- The Pydantic `ModelSpec` model validates each entry against `providers/llm/models.json` — unknown model names log a warning; setting `reasoning: low|medium|high` on a model whose registry entry has `thinking: false` is a hard error.
- `resolve_model_specs(spec)` returns `list[ModelSpec]` from either a single dict or a list (list = fallback chain, tried in order).

## `youtube` pull source — sugar over `feed`

A `pull: - youtube: {channel_id, name?, …}` item is **not** a separate `Source`. `get_feeds` expands it via `_youtube_to_feed` into a normal `feed` dict — the URL is built from `channel_id` as `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` (byte-identical to the verbose form, so feed state keyed by url is preserved). All five `get_feeds` callers (processing, due-checks, regen-state, stats) and the whole RSS pipeline therefore work unchanged; `task_kind` returns `feeds` for a youtube-only task. `_PullYouTubeItem` has full feed parity (`discord`, `ignore`, `skip`, `description`, `title`, `summarize`, `curate`) with `channel_id` replacing `url`; `_youtube_to_feed` carries every key through unchanged (no key lifting). The entry is itself a YouTube feed, so its `ignore`/`skip` apply directly and the global `youtube:` scope merges in.

**Naming:** `youtube` is used in two distinct schema positions — the _pull source_ (`pull: - youtube:`, model `_PullYouTubeItem`) and the _scope block_ (`youtube: {ignore, skip}` at global/task/feed, model `_YouTube`, applied only to YouTube feeds via `is_youtube_feed_url`). Different models, different locations; not a conflict.

## `scope.py` — layered config resolution

`layer_dict(*blocks)` is **the** primitive for settings that can be set at more than one scope (global → task → feed), each more specific scope overriding the broader one per leaf key. It shallow-merges the blocks in low→high precedence order, skipping non-dict (`None`/absent) blocks so callers can pass `cfg.get("ignore")` directly.

Callers extract each scope's block themselves, because accessors differ — e.g. the task-level Discord block lives under `push[].discord` (`get_discord_cfg`), not `task["discord"]`:

```python
color = parse_color(layer_dict(global_cfg.get("discord"), get_discord_cfg(task_cfg), fc.get("discord")).get("color"))
```

Used in `tasks/feeds.py` for `ignore`, `skip`, `description`, `title` (all via `resolve_scoped`), and `discord` (`.color`). `resolve_scoped(key, …, youtube=bool)` adds the YouTube-scope layers: for a feed where `is_youtube_feed_url(url)` is true it interleaves the global/task `youtube.<key>` blocks at the matching precedence, so a global `youtube.ignore.description` is overridable per task/feed. `language` is the deliberate exception — it is sourced from different parent blocks at different scopes (`curate.language` globally vs feed-level) and doesn't reduce to one keyed merge, so it stays a `RunContext.language` property read at the curate call site.

## `config.yaml.template`

Canonical reference for all supported config keys and defaults.

Any change that adds, removes, or renames a config key must also update the Pydantic model in `schema.py` so validation stays in sync.
