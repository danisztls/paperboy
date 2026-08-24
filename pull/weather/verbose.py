# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Default (verbose) forecast formatter: full daily summary + hourly rows."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pull.weather.common import (
    _DISPLAY_HOURS,
    day_value,
    find_hourly_index,
    find_today_idx,
    header_line,
    uv_label,
    uv_window,
    weekday_pt,
)

# Precip cutoffs: hide the precip display below this probability / volume.
_PRECIP_HIDE_PROB = 20  # %
_PRECIP_HIDE_MM = 1.0  # mm


def _format_today(
    day_idx: int,
    today_str: str,
    location_name: str,
    daily: dict,
    hourly: dict,
    *,
    uv_threshold: int,
) -> list[str]:
    def _d(key: str):
        return day_value(daily, key, day_idx)

    feels_min = int(round(_d("apparent_temperature_min") or 0))
    feels_max = int(round(_d("apparent_temperature_max") or 0))
    precip_mm = int(round(_d("precipitation_sum") or 0))
    precip_prob = int(round(_d("precipitation_probability_max") or 0))
    uv_max = _d("uv_index_max") or 0.0
    wind_speed = int(round(_d("wind_speed_10m_max") or 0))
    wind_gust = int(round(_d("wind_gusts_10m_max") or 0))

    lines = [
        header_line(_d("weather_code"), location_name, today_str),
        "",
        f"🌡 ↓{feels_min}°C  ↑{feels_max}°C   💧 {precip_mm}mm ({precip_prob}%)",
        f"💨 {wind_speed}km/h (raj. {wind_gust}km/h)",
    ]

    start_h, end_h, peak_uv = uv_window(hourly, today_str, uv_threshold)
    if start_h is not None and end_h is not None:
        label = uv_label(uv_max)
        lines.append(f"🔆 UV {label} {start_h}h–{end_h}h (pico {int(round(peak_uv))})")

    lines.append("")

    times: list[str] = hourly.get("time", [])
    h_feels = hourly.get("apparent_temperature", [])
    h_prob = hourly.get("precipitation_probability", [])

    entries = []
    for h in _DISPLAY_HOURS:
        idx = find_hourly_index(times, today_str, h)
        if idx is None or idx >= len(h_feels):
            continue
        feels = int(round(h_feels[idx] or 0))
        prob = int(round(h_prob[idx] if idx < len(h_prob) else 0) or 0)
        precip_part = f" 💧{prob}%" if prob >= _PRECIP_HIDE_PROB else ""
        entries.append(f"**{h:02d}h**: {feels}°C{precip_part}")
    if entries:
        lines.append("  ·  ".join(entries))

    return lines


def _format_forecast(start_idx: int, daily: dict, forecast_days: int) -> list[str]:
    times: list[str] = daily.get("time", [])
    d_min = daily.get("apparent_temperature_min", [])
    d_max = daily.get("apparent_temperature_max", [])
    d_precip = daily.get("precipitation_sum", [])
    d_prob = daily.get("precipitation_probability_max", [])

    entries = []
    for i in range(1, forecast_days):
        idx = start_idx + i
        if idx >= len(times):
            break
        weekday = weekday_pt(times[idx])
        feels_min = int(round(d_min[idx] if idx < len(d_min) else 0) or 0)
        feels_max = int(round(d_max[idx] if idx < len(d_max) else 0) or 0)
        mm = d_precip[idx] if idx < len(d_precip) else 0.0
        prob = int(round(d_prob[idx] if idx < len(d_prob) else 0) or 0)
        precip_part = (
            f" 💧{int(round(mm))}mm {prob}%"
            if (prob >= _PRECIP_HIDE_PROB and mm >= _PRECIP_HIDE_MM)
            else ""
        )
        entries.append(f"**{weekday}** ↓{feels_min}°C ↑{feels_max}°C{precip_part}")
    if not entries:
        return []
    return ["", "**Próximos dias**", "  ·  ".join(entries)]


def format_message(data: dict, cfg: dict) -> str:
    tz = ZoneInfo(cfg["timezone"])
    daily = data["daily"]
    hourly = data["hourly"]

    day_idx = find_today_idx(daily, tz)
    today_str = daily["time"][day_idx]

    lines = _format_today(
        day_idx,
        today_str,
        cfg.get("location_name", "?"),
        daily,
        hourly,
        uv_threshold=cfg.get("uv_warn_threshold", 6),
    )
    lines += _format_forecast(day_idx, daily, cfg.get("forecast_days", 7))
    return "\n".join(lines)
