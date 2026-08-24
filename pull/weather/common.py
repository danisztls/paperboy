# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Shared helpers for both weather formatters (verbose + smart)."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger(__name__)

_DISPLAY_HOURS = tuple(range(5, 24, 2))  # 5, 7, 9 … 23 — every 2h

_PT_DAYS = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]

_WMO_EMOJI: dict[int, str] = {
    0: "☀",
    1: "🌤",
    2: "⛅",
    3: "☁",
    45: "🌫",
    48: "🌫",
    51: "🌦",
    53: "🌦",
    55: "🌦",
    56: "🌨",
    57: "🌨",
    61: "🌧",
    63: "🌧",
    65: "🌧",
    66: "🌨",
    67: "🌨",
    71: "❄",
    73: "❄",
    75: "❄",
    77: "❄",
    80: "🌧",
    81: "🌧",
    82: "🌧",
    85: "❄",
    86: "❄",
    95: "⛈",
    96: "⛈",
    99: "⛈",
}


def wmo_emoji(code: int | None) -> str:
    if code is None:
        return "🌡"
    return _WMO_EMOJI.get(int(code), "🌡")


def uv_label(uv: float) -> str:
    if uv < 3:
        return "baixo"
    if uv < 6:
        return "moderado"
    if uv < 8:
        return "alto"
    if uv < 11:
        return "muito alto"
    return "extremo"


def weekday_pt(date_str: str) -> str:
    """'YYYY-MM-DD' → Portuguese weekday abbreviation ('Dom'…'Sáb'), '?' if unparseable."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "?"
    return _PT_DAYS[(dt.weekday() + 1) % 7]


def header_line(wcode: int | None, location_name: str, today_str: str) -> str:
    dd_mm = today_str[8:10] + "/" + today_str[5:7]
    return f"### {wmo_emoji(wcode)} {location_name} · {weekday_pt(today_str)} {dd_mm}"


def day_value(daily: dict, key: str, idx: int):
    vals = daily.get(key, [])
    return vals[idx] if idx < len(vals) else None


def find_today_idx(daily: dict, tz: ZoneInfo) -> int:
    today = datetime.now(tz).date().isoformat()
    times: list[str] = daily.get("time", [])
    try:
        return times.index(today)
    except ValueError:
        return 0


def find_hourly_index(times: list[str], date_str: str, hour: int) -> int | None:
    target = f"{date_str}T{hour:02d}:00"
    try:
        return times.index(target)
    except ValueError:
        return None


def _hourly_window(
    hourly: dict,
    values_key: str,
    date_str: str,
    hours,
    threshold: float,
) -> tuple[int | None, int | None, float]:
    """First contiguous block of `hours` where hourly[values_key] >= threshold.

    Returns (start_hour, end_hour, peak) — (None, None, 0.0) when nothing fires.
    """
    times: list[str] = hourly.get("time", [])
    values: list = hourly.get(values_key, [])
    start_h: int | None = None
    end_h: int | None = None
    peak = 0.0
    in_window = False
    for h in hours:
        idx = find_hourly_index(times, date_str, h)
        if idx is None or idx >= len(values):
            continue
        v = values[idx] or 0.0
        if v >= threshold:
            if not in_window:
                start_h = h
                in_window = True
            end_h = h
            if v > peak:
                peak = v
        elif in_window:
            break  # first contiguous block only
    return start_h, end_h, peak


def uv_window(
    hourly: dict, today_str: str, uv_threshold: int
) -> tuple[int | None, int | None, float]:
    return _hourly_window(hourly, "uv_index", today_str, _DISPLAY_HOURS, uv_threshold)


def threshold_windows(
    hourly: dict,
    values_key: str,
    date_str: str,
    predicate,
    *,
    min_hours: int = 1,
) -> list[tuple[int, int, float]]:
    """All contiguous blocks over the full 24h where predicate(value) holds.

    Scans hours 0..23 of `hourly[values_key]`. Returns a list of
    (start_hour, end_hour, peak) tuples (end inclusive); `peak` is the max value
    in the block. Blocks shorter than `min_hours` are dropped. A missing index or
    a None reading breaks the current block.

    The shared interval primitive for the smart formatter — hot/cold (apparent
    temp), rain (precipitation probability), and UV all run through this so they
    report every block, not just the first, with one min-window rule.
    """
    times: list[str] = hourly.get("time", [])
    values: list = hourly.get(values_key, [])

    windows: list[tuple[int, int, float]] = []
    start: int | None = None
    end: int | None = None
    peak = 0.0

    def _flush() -> None:
        if start is not None and end is not None and end - start + 1 >= min_hours:
            windows.append((start, end, peak))

    for h in range(24):
        idx = find_hourly_index(times, date_str, h)
        if idx is None or idx >= len(values):
            _flush()
            start = None
            end = None
            continue
        v = values[idx]
        if v is not None and predicate(v):
            if start is None:
                start = h
                peak = v
            end = h
            if v > peak:
                peak = v
        else:
            _flush()
            start = None
            end = None

    _flush()
    return windows


def daily_humidity_mean(hourly: dict, date_str: str) -> float | None:
    times: list[str] = hourly.get("time", [])
    hums: list = hourly.get("relative_humidity_2m", [])
    values: list[float] = []
    for i, t in enumerate(times):
        if not t.startswith(date_str):
            continue
        if i >= len(hums) or hums[i] is None:
            continue
        values.append(hums[i])
    if not values:
        return None
    return sum(values) / len(values)


async def get_json(session: aiohttp.ClientSession, url: str, label: str) -> dict | None:
    """GET a JSON API endpoint; logs and returns None on HTTP/network failure."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.error("[weather] %s returned HTTP %s", label, resp.status)
                return None
            return await resp.json()
    except Exception as exc:
        log.error("[weather] %s HTTP error: %s", label, exc)
        return None
