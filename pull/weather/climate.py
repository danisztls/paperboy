"""Climate normals from the Open-Meteo Archive API (ERA5 reanalysis).

Monthly cache of μ + σ for apparent max/min and daily-mean humidity over the
current calendar month across the last CLIMATE_NORMAL_YEARS years. The smart
formatter uses it as the `hist` anomaly baseline.
"""

from __future__ import annotations

import calendar
import logging
import statistics
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import aiohttp

from pull.weather.common import get_json

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CLIMATE_NORMAL_YEARS = 5  # rolling window for archive-API normals


def climate_cache_fresh(cache: dict | None, now_local: datetime) -> bool:
    if not cache or "month" not in cache:
        return False
    if cache["month"] != now_local.strftime("%Y-%m"):
        return False
    return "apparent_max_std" in cache


async def fetch_climate_normals(cfg: dict, session: aiohttp.ClientSession) -> dict | None:
    """Fetch monthly climate normals (μ and σ) from Open-Meteo Archive.

    Returns the mean and sample standard deviation for apparent max/min and
    daily-mean relative humidity, computed over the current calendar month
    across the past CLIMATE_NORMAL_YEARS years (excluding the current year,
    which the archive can't fully serve). Returns None on network/parse
    failure — callers must tolerate this.
    """
    tz = ZoneInfo(cfg["timezone"])
    now_local = datetime.now(tz)
    year = now_local.year
    month = now_local.month

    start_year = year - CLIMATE_NORMAL_YEARS
    end_year = year - 1
    last_day = calendar.monthrange(end_year, month)[1]

    params = [
        ("latitude", cfg["latitude"]),
        ("longitude", cfg["longitude"]),
        ("start_date", f"{start_year}-{month:02d}-01"),
        ("end_date", f"{end_year}-{month:02d}-{last_day:02d}"),
        ("daily", "apparent_temperature_max,apparent_temperature_min"),
        ("hourly", "relative_humidity_2m"),
        ("timezone", cfg["timezone"]),
    ]
    qs = "&".join(f"{k}={v}" for k, v in params)
    data = await get_json(session, f"{ARCHIVE_URL}?{qs}", "Archive API")
    if data is None:
        return None

    daily = data.get("daily") or {}
    hourly = data.get("hourly") or {}
    target = f"-{month:02d}-"

    def _daily_stats(key: str) -> tuple[float, float] | None:
        times = daily.get("time", [])
        vals = daily.get(key, [])
        kept: list[float] = []
        for i, t in enumerate(times):
            if target not in t:
                continue
            if i >= len(vals) or vals[i] is None:
                continue
            kept.append(vals[i])
        if len(kept) < 2:
            return None
        return (statistics.fmean(kept), statistics.stdev(kept))

    h_times = hourly.get("time", [])
    h_hums = hourly.get("relative_humidity_2m", [])
    per_day: dict[str, list[float]] = {}
    for i, t in enumerate(h_times):
        date_part = t[:10]
        if target not in date_part:
            continue
        if i >= len(h_hums) or h_hums[i] is None:
            continue
        per_day.setdefault(date_part, []).append(h_hums[i])
    daily_hum_means = [sum(vs) / len(vs) for vs in per_day.values() if vs]
    if len(daily_hum_means) >= 2:
        humidity_mean = statistics.fmean(daily_hum_means)
        humidity_std = statistics.stdev(daily_hum_means)
    else:
        humidity_mean = None
        humidity_std = None

    apparent_max = _daily_stats("apparent_temperature_max")
    apparent_min = _daily_stats("apparent_temperature_min")
    if apparent_max is None or apparent_min is None:
        log.error("[weather] Archive response missing apparent-temp data")
        return None

    return {
        "month": now_local.strftime("%Y-%m"),
        "fetched_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "apparent_max_mean": round(apparent_max[0], 1),
        "apparent_max_std": round(apparent_max[1], 2),
        "apparent_min_mean": round(apparent_min[0], 1),
        "apparent_min_std": round(apparent_min[1], 2),
        "humidity_mean": round(humidity_mean, 1) if humidity_mean is not None else None,
        "humidity_std": round(humidity_std, 2) if humidity_std is not None else None,
    }
