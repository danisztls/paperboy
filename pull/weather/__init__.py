# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Open-Meteo weather source.

`WeatherSource` fetches the forecast (no auth) and formats `Item.body` via one
of two formatters picked by `cfg["kind"]`: `verbose.format_message` (default)
or `smart.format_smart_message` (signal-only, σ-anomaly gated — see smart.py).
The forecast URL passes `past_days=7` so smart mode's recent baseline rides on
the same HTTP call.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import aiohttp

from pipeline import Item, PullResult, Source
from pull.weather.climate import climate_cache_fresh, fetch_climate_normals
from pull.weather.common import find_today_idx, get_json
from pull.weather.smart import format_smart_message
from pull.weather.verbose import format_message

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

_DAILY_VARS = ",".join(
    [
        "apparent_temperature_max",
        "apparent_temperature_min",
        "precipitation_sum",
        "precipitation_probability_max",
        "uv_index_max",
        "weather_code",
        "wind_speed_10m_max",
        "wind_gusts_10m_max",
        "sunrise",
        "sunset",
    ]
)

_HOURLY_VARS = ",".join(
    [
        "apparent_temperature",
        "precipitation_probability",
        "precipitation",
        "relative_humidity_2m",
        "uv_index",
    ]
)


def build_url(cfg: dict) -> str:
    params = [
        ("latitude", cfg["latitude"]),
        ("longitude", cfg["longitude"]),
        ("daily", _DAILY_VARS),
        ("hourly", _HOURLY_VARS),
        ("timezone", cfg["timezone"]),
        ("forecast_days", cfg.get("forecast_days", 7)),
        ("past_days", 7),
        ("models", "best_match"),
    ]
    qs = "&".join(f"{k}={v}" for k, v in params)
    return f"{OPEN_METEO_URL}?{qs}"


class WeatherSource(Source):
    async def pull(
        self,
        cfg: dict,
        seen: set[str],
        session: aiohttp.ClientSession,
    ) -> PullResult | None:
        url = build_url(cfg)
        data = await get_json(session, url, "Open-Meteo")
        if data is None:
            return None

        if "daily" not in data or "hourly" not in data:
            log.error("[weather] Unexpected response shape: %s", list(data.keys()))
            return None

        try:
            if cfg.get("kind") == "smart":
                body = format_smart_message(data, cfg, cfg.get("_climate_normals"))
            else:
                body = format_message(data, cfg)
        except Exception as exc:
            log.error("[weather] Format error: %s", exc)
            return None

        tz = ZoneInfo(cfg["timezone"])
        today_str = data["daily"]["time"][find_today_idx(data["daily"], tz)]
        location_name = cfg.get("location_name", "?")

        item = Item(
            id=today_str,
            title=location_name,
            source="weather",
            url=url,
            body=body,
        )
        return PullResult(
            new_items=[item],
            current_items=[{"url": url, "title": location_name}],
        )


__all__ = [
    "WeatherSource",
    "build_url",
    "climate_cache_fresh",
    "fetch_climate_normals",
]
