# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Smart (signal-only) formatter: lines fire only on rain thresholds or σ-anomalies.

Anomalies are checked against two frames with OR semantics: hist (climate-month
normals, `SIGMA_HIST` σ) and recent (past 7 days, `SIGMA_RECENT` σ). The
stronger frame renders as e.g. `(+5° vs normal 28°)` / `(+3° vs semana 30°)`;
σ-multipliers drive the decision but are omitted from the display.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pull.weather.common import (
    daily_humidity_mean,
    day_value,
    find_today_idx,
    header_line,
    threshold_windows,
    uv_label,
    weekday_pt,
)

# Thresholds. Module-level so a future config knob is a one-line change.
RAIN_TODAY_PROB_MIN = 30
RAIN_TODAY_MM_MIN = 1.0
RAIN_NEXT_PROB_MIN = 60
RAIN_NEXT_MM_MIN = 5.0
SIGMA_HIST = 3.0  # σ threshold vs climate-month mean (5-year window)
SIGMA_RECENT = 2.0  # σ threshold vs past-7-days mean
SIGMA_FLOOR = 0.1  # avoid divide-by-near-zero when years align
RECENT_MIN_SAMPLES = 4  # skip practical trigger if past window has fewer days
COMFORT_TEMP_MIN = 18  # °C apparent — below this it's "frio" (you'd want a jacket)
COMFORT_TEMP_MAX = 27  # °C apparent — above this it's "quente" (you start sweating)
MIN_WINDOW_HOURS = 2  # skip blips shorter than this (hot/cold/UV; rain keeps 1h resolution)
# Golden hour: sun altitude span ~10° (extended definition, -4° to +6°). Sun's
# altitude near horizon changes at ~15° × cos(lat) per hour, so duration ≈
# 40 / cos(lat) min. Capped at 180 min for polar latitudes.
GOLDEN_HOUR_ARC_DEG = 10
GOLDEN_HOUR_MAX_MINUTES = 180


def _join_windows(blocks: list[tuple[int, int, float]]) -> str:
    """Render blocks as `10h–16h, 19h–21h`; a single-hour block collapses to `10h`."""
    return ", ".join(f"{s}h" if s == e else f"{s}h–{e}h" for s, e, _ in blocks)


def _golden_hour_minutes(latitude: float) -> int:
    """Approximate golden-hour duration in minutes for a given latitude.

    Sun altitude near the horizon changes at ~15° × cos(lat) per hour; covering
    the ~GOLDEN_HOUR_ARC_DEG arc takes proportionally longer at higher latitude.
    Capped at GOLDEN_HOUR_MAX_MINUTES — past that the concept stops being a
    "window" anyway.
    """
    rate = math.cos(math.radians(latitude))
    if rate < 0.05:  # ~> 87° latitude — sun barely moves vertically
        return GOLDEN_HOUR_MAX_MINUTES
    return min(GOLDEN_HOUR_MAX_MINUTES, int(round(GOLDEN_HOUR_ARC_DEG * 4 / rate)))


def _golden_hours(daily: dict, day_idx: int, latitude: float) -> tuple[str, str] | None:
    """Return (morning, evening) golden-hour 'HH:MM–HH:MM' strings, or None.

    Open-Meteo returns sunrise/sunset in the requested local timezone (no offset
    suffix). The morning window runs from sunrise to sunrise + duration; the
    evening window runs from sunset − duration to sunset. Duration is computed
    from latitude via `_golden_hour_minutes`.
    """
    sunrises = daily.get("sunrise", [])
    sunsets = daily.get("sunset", [])
    if day_idx >= len(sunrises) or day_idx >= len(sunsets):
        return None
    sr_str = sunrises[day_idx]
    ss_str = sunsets[day_idx]
    if not sr_str or not ss_str:
        return None
    try:
        sr = datetime.fromisoformat(sr_str)
        ss = datetime.fromisoformat(ss_str)
    except ValueError, TypeError:
        return None
    delta = timedelta(minutes=_golden_hour_minutes(latitude))
    morning = f"{sr:%H:%M}–{(sr + delta):%H:%M}"
    evening = f"{(ss - delta):%H:%M}–{ss:%H:%M}"
    return (morning, evening)


def _format_smart_today(
    day_idx: int,
    today_str: str,
    location_name: str,
    daily: dict,
    hourly: dict,
    *,
    uv_threshold: int,
    latitude: float,
) -> list[str]:
    def _d(key: str):
        return day_value(daily, key, day_idx)

    feels_min = int(round(_d("apparent_temperature_min") or 0))
    feels_max = int(round(_d("apparent_temperature_max") or 0))
    precip_mm = _d("precipitation_sum") or 0.0
    precip_prob = int(round(_d("precipitation_probability_max") or 0))
    uv_max = _d("uv_index_max") or 0.0

    lines = [
        header_line(_d("weather_code"), location_name, today_str),
        "",
        f"🌡 sensação ↓{feels_min}°C  ↑{feels_max}°C",
    ]

    uv = threshold_windows(
        hourly, "uv_index", today_str, lambda v: v >= uv_threshold, min_hours=MIN_WINDOW_HOURS
    )
    if uv:
        peak = max(p for _, _, p in uv)
        lines.append(f"🔆 UV {uv_label(uv_max)} {_join_windows(uv)} (pico {int(round(peak))})")

    if precip_prob >= RAIN_TODAY_PROB_MIN and precip_mm >= RAIN_TODAY_MM_MIN:
        # Rain keeps 1h resolution — a lone high-probability hour is worth flagging.
        rain = threshold_windows(
            hourly,
            "precipitation_probability",
            today_str,
            lambda v: v >= RAIN_TODAY_PROB_MIN,
            min_hours=1,
        )
        window_part = f" {_join_windows(rain)}" if rain else ""
        lines.append(f"💧 {int(round(precip_mm))}mm {precip_prob}% chuva{window_part}")

    hot = threshold_windows(
        hourly,
        "apparent_temperature",
        today_str,
        lambda f: f > COMFORT_TEMP_MAX,
        min_hours=MIN_WINDOW_HOURS,
    )
    if hot:
        lines.append("🥵 quente " + _join_windows(hot))
    cold = threshold_windows(
        hourly,
        "apparent_temperature",
        today_str,
        lambda f: f < COMFORT_TEMP_MIN,
        min_hours=MIN_WINDOW_HOURS,
    )
    if cold:
        lines.append("🥶 frio " + _join_windows(cold))

    golden = _golden_hours(daily, day_idx, latitude)
    if golden is not None:
        lines.append(f"🌇 hora dourada {golden[0]}, {golden[1]}")

    return lines


def _baseline_from_normals(normals: dict | None) -> dict[str, tuple[float, float] | None]:
    """Extract (μ, σ) tuples per metric from a climate-normal cache entry."""
    if not normals:
        return {}

    def _pair(mean_key: str, std_key: str) -> tuple[float, float] | None:
        mu = normals.get(mean_key)
        sigma = normals.get(std_key)
        if mu is None or sigma is None:
            return None
        return (mu, sigma)

    return {
        "apparent_max": _pair("apparent_max_mean", "apparent_max_std"),
        "apparent_min": _pair("apparent_min_mean", "apparent_min_std"),
        "humidity": _pair("humidity_mean", "humidity_std"),
    }


def _recent_baseline(
    daily: dict, hourly: dict, day_idx: int
) -> dict[str, tuple[float, float] | None]:
    """Compute (μ, σ) over the 7 daily indices immediately before today.

    Returned `None` for any metric with fewer than RECENT_MIN_SAMPLES valid
    values — too few points to draw a meaningful σ from.
    """
    times: list[str] = daily.get("time", [])
    start = max(0, day_idx - 7)

    def _stats(arr: list) -> tuple[float, float] | None:
        vals: list[float] = []
        for i in range(start, day_idx):
            if i < 0 or i >= len(arr) or arr[i] is None:
                continue
            vals.append(arr[i])
        if len(vals) < RECENT_MIN_SAMPLES:
            return None
        return (statistics.fmean(vals), statistics.stdev(vals))

    hum_vals: list[float] = []
    for i in range(start, day_idx):
        if i < 0 or i >= len(times):
            continue
        hm = daily_humidity_mean(hourly, times[i])
        if hm is not None:
            hum_vals.append(hm)
    if len(hum_vals) >= RECENT_MIN_SAMPLES:
        hum_pair = (statistics.fmean(hum_vals), statistics.stdev(hum_vals))
    else:
        hum_pair = None

    return {
        "apparent_max": _stats(daily.get("apparent_temperature_max", [])),
        "apparent_min": _stats(daily.get("apparent_temperature_min", [])),
        "humidity": hum_pair,
    }


def _evaluate_anomaly(
    value: float,
    hist: tuple[float, float] | None,
    recent: tuple[float, float] | None,
) -> dict | None:
    """Decide whether `value` is anomalous vs hist (≥SIGMA_HIST) or recent (≥SIGMA_RECENT).

    Returns None if neither frame fires. Otherwise a dict with both signed
    σ-multipliers (when computable), the means, and which frame fired. The
    caller picks the stronger frame for rendering.
    """
    hist_mult: float | None = None
    recent_mult: float | None = None
    if hist is not None:
        mu, sigma = hist
        hist_mult = (value - mu) / max(sigma, SIGMA_FLOOR)
    if recent is not None:
        mu, sigma = recent
        recent_mult = (value - mu) / max(sigma, SIGMA_FLOOR)

    hist_fired = hist_mult is not None and abs(hist_mult) >= SIGMA_HIST
    recent_fired = recent_mult is not None and abs(recent_mult) >= SIGMA_RECENT
    if not (hist_fired or recent_fired):
        return None

    h_mag = abs(hist_mult) if hist_fired else 0.0
    r_mag = abs(recent_mult) if recent_fired else 0.0
    primary = "hist" if h_mag >= r_mag else "recent"

    return {
        "primary": primary,
        "hist_mult": hist_mult,
        "recent_mult": recent_mult,
        "hist_mu": hist[0] if hist is not None else None,
        "recent_mu": recent[0] if recent is not None else None,
        "hist_fired": hist_fired,
        "recent_fired": recent_fired,
    }


def _render_anomaly_suffix(decision: dict, value: float, *, unit: str) -> str:
    """Format the '(+5° vs normal 28°)' parenthetical from a decision.

    Frame label follows the stronger trigger; σ-multipliers are computed but
    intentionally omitted from the display to keep the line scannable.
    """
    if decision["primary"] == "hist":
        mu = decision["hist_mu"]
        label = "normal"
    else:
        mu = decision["recent_mu"]
        label = "semana"

    delta = value - mu
    sign = "+" if delta > 0 else ""
    return f"({sign}{int(round(delta))}{unit} vs {label} {int(round(mu))}{unit})"


def _render_anomaly_temp_line(qualifier: str, value: float, decision: dict) -> str:
    primary_mu = decision["hist_mu"] if decision["primary"] == "hist" else decision["recent_mu"]
    emoji = "🔥" if value > primary_mu else "🧊"
    suffix = _render_anomaly_suffix(decision, value, unit="°")
    return f"{emoji} sensação {qualifier} {int(round(value))}°C {suffix}"


def _render_anomaly_humidity_line(value: float, decision: dict) -> str:
    suffix = _render_anomaly_suffix(decision, value, unit="%")
    return f"💦 humidade {int(round(value))}% {suffix}"


def _decision_magnitude(decision: dict) -> float:
    h = abs(decision["hist_mult"]) if decision["hist_fired"] else 0.0
    r = abs(decision["recent_mult"]) if decision["recent_fired"] else 0.0
    return max(h, r)


def _pick_apparent_anomaly(
    idx: int,
    d_app_max: list,
    d_app_min: list,
    hist_baseline: dict,
    recent_baseline: dict,
) -> str | None:
    """Pick whichever of (máx, mín) has the strongest σ-anomaly and render its line."""
    candidates: list[tuple[float, str]] = []
    pairs = [
        ("máx", d_app_max, "apparent_max"),
        ("mín", d_app_min, "apparent_min"),
    ]
    for qualifier, arr, key in pairs:
        if idx >= len(arr) or arr[idx] is None:
            continue
        value = arr[idx]
        decision = _evaluate_anomaly(value, hist_baseline.get(key), recent_baseline.get(key))
        if decision is None:
            continue
        candidates.append(
            (_decision_magnitude(decision), _render_anomaly_temp_line(qualifier, value, decision))
        )

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _format_smart_forecast(
    start_idx: int,
    daily: dict,
    hourly: dict,
    forecast_days: int,
    normals: dict | None,
) -> list[str]:
    times: list[str] = daily.get("time", [])
    d_app_min = daily.get("apparent_temperature_min", [])
    d_app_max = daily.get("apparent_temperature_max", [])
    d_precip = daily.get("precipitation_sum", [])
    d_prob = daily.get("precipitation_probability_max", [])

    hist_baseline = _baseline_from_normals(normals)
    recent_baseline = _recent_baseline(daily, hourly, start_idx)

    lines: list[str] = []
    for i in range(1, forecast_days):
        idx = start_idx + i
        if idx >= len(times):
            break
        date_str = times[idx]
        parts: list[str] = []

        mm = d_precip[idx] if idx < len(d_precip) else 0.0
        prob = int(round(d_prob[idx] if idx < len(d_prob) else 0) or 0)
        if mm >= RAIN_NEXT_MM_MIN and prob >= RAIN_NEXT_PROB_MIN:
            # Rain keeps 1h resolution — a lone high-probability hour is worth flagging.
            rain = threshold_windows(
                hourly,
                "precipitation_probability",
                date_str,
                lambda v: v >= RAIN_NEXT_PROB_MIN,
                min_hours=1,
            )
            window_part = f" {_join_windows(rain)}" if rain else ""
            parts.append(f"💧 {int(round(mm))}mm {prob}%{window_part}")

        anomaly = _pick_apparent_anomaly(idx, d_app_max, d_app_min, hist_baseline, recent_baseline)
        if anomaly:
            parts.append(anomaly)

        day_hum = daily_humidity_mean(hourly, date_str)
        if day_hum is not None:
            decision = _evaluate_anomaly(
                day_hum, hist_baseline.get("humidity"), recent_baseline.get("humidity")
            )
            if decision is not None:
                parts.append(_render_anomaly_humidity_line(day_hum, decision))

        if parts:
            lines.append(f"**{weekday_pt(date_str)}** " + "  ".join(parts))

    if not lines:
        return []
    return ["", "**Próximos dias**", *lines]


def format_smart_message(data: dict, cfg: dict, normals: dict | None) -> str:
    tz = ZoneInfo(cfg["timezone"])
    daily = data["daily"]
    hourly = data["hourly"]

    day_idx = find_today_idx(daily, tz)
    today_str = daily["time"][day_idx]

    lines = _format_smart_today(
        day_idx,
        today_str,
        cfg.get("location_name", "?"),
        daily,
        hourly,
        uv_threshold=cfg.get("uv_warn_threshold", 6),
        latitude=cfg["latitude"],
    )
    lines += _format_smart_forecast(day_idx, daily, hourly, cfg.get("forecast_days", 7), normals)
    return "\n".join(lines)
