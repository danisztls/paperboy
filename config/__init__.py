# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Config loading, primitives/accessors, and Pydantic validation.

- `values` — `Period`, parsers, `task_kind`, and the raw-dict accessors (`get_*`).
- `schema` — Pydantic models; `validate_config` returns a list of error strings.
- `loader` — `load_config` (YAML/JSON + `!secret`).
- `scope` — `layer_dict` / `resolve_scoped` for global→task→feed layered blocks.
"""

from config.loader import load_config
from config.schema import ModelSpec, resolve_model_specs, validate_config
from config.values import (
    Period,
    get_api_key_for_provider,
    get_discord_cfg,
    get_feeds,
    get_file_path,
    get_finance_cfg,
    get_realestate_cfgs,
    get_research_cfg,
    get_weather_cfg,
    is_youtube_feed_url,
    parse_color,
    parse_period,
    task_kind,
)

__all__ = [
    "ModelSpec",
    "Period",
    "get_api_key_for_provider",
    "get_discord_cfg",
    "get_feeds",
    "get_file_path",
    "get_finance_cfg",
    "get_realestate_cfgs",
    "get_research_cfg",
    "get_weather_cfg",
    "is_youtube_feed_url",
    "load_config",
    "parse_color",
    "parse_period",
    "resolve_model_specs",
    "task_kind",
    "validate_config",
]
