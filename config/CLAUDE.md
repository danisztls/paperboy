# config/

Config loading and validation.

## `__init__.py`

- `load_config(path)` — reads YAML or JSON.
- `validate_config(config)` — uses Pydantic models to validate the full config, returns a list of error strings.
- Global LLM config is split into four top-level sections:
  - `llm` (API keys only, under `llm.api_key`)
  - `curate`, `search`, `summarize` (each can carry its own `model` spec)
- Task/feed-level `curate.model` etc. override the matching global section.
- Model specs are verbose dicts: `{provider, name, reasoning?}` where provider ∈ `{openai, gemini, anthropic, deepseek}` and reasoning ∈ `{off, low, medium, high}` (absent = off).
- The Pydantic `ModelSpec` model validates each entry against `providers/llm/models.json` — unknown model names log a warning; setting `reasoning: low|medium|high` on a model whose registry entry has `thinking: false` is a hard error.
- `resolve_model_specs(spec)` returns `list[ModelSpec]` from either a single dict or a list (list = fallback chain, tried in order).
- Other helpers: `parse_color`, `parse_period`, `task_kind` (returns explicit `kind:` key if present; otherwise infers from `pull` list: `realestate` item → realestate, `search` item → search, else feeds), `get_api_key_for_provider`, `get_feeds`, `get_discord_cfg`, `get_search_cfg`, `_get_realestate_cfgs`, `get_file_path`.

## `scope.py` — layered config resolution

`layer_dict(*blocks)` is **the** primitive for settings that can be set at more than one scope (global → task → feed), each more specific scope overriding the broader one per leaf key. It shallow-merges the blocks in low→high precedence order, skipping non-dict (`None`/absent) blocks so callers can pass `cfg.get("filter")` directly.

Callers extract each scope's block themselves, because accessors differ — e.g. the task-level Discord block lives under `push[].discord` (`get_discord_cfg`), not `task["discord"]`:

```python
color = parse_color(layer_dict(global_cfg.get("discord"), get_discord_cfg(task_cfg), fc.get("discord")).get("color"))
```

Used in `tasks.py` for `filter`, `image` (`.skip`), `discord` (`.color`), and `youtube`. `language` is the deliberate exception — it is sourced from different parent blocks at different scopes (`curate.language` globally vs feed-level `filter.language`) and doesn't reduce to one keyed merge, so it keeps its own `global_language` threading.

## `config.yaml.template`

Canonical reference for all supported config keys and defaults.

Any change that adds, removes, or renames a config key must also update the Pydantic model here so validation stays in sync.
