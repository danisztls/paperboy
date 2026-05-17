from __future__ import annotations

import calendar
import logging
import math
import statistics
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from pipeline import Item, PullResult, Source

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

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

_DISPLAY_HOURS = tuple(range(5, 24, 2))  # 5, 7, 9 … 23 — every 2h

_PT_DAYS = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]

# Verbose-mode precip cutoffs (existing behavior).
_PRECIP_HIDE_PROB = 20  # % — hide precip display below this probability
_PRECIP_HIDE_MM = 1.0  # mm — hide precip display below this volume

# Smart-mode thresholds. Module-level so a future config knob is a one-line change.
RAIN_TODAY_PROB_MIN = 30
RAIN_TODAY_MM_MIN = 1.0
RAIN_NEXT_PROB_MIN = 60
RAIN_NEXT_MM_MIN = 5.0
SIGMA_HIST = 3.0  # σ threshold vs climate-month mean (5-year window)
SIGMA_RECENT = 2.0  # σ threshold vs past-7-days mean
SIGMA_FLOOR = 0.1  # avoid divide-by-near-zero when years align
RECENT_MIN_SAMPLES = 4  # skip practical trigger if past window has fewer days
CLIMATE_NORMAL_YEARS = 5  # rolling window for archive-API normals
COMFORT_TEMP_MIN = 18  # °C apparent — below this you'd want a jacket
COMFORT_TEMP_MAX = 27  # °C apparent — above this you start sweating
COMFORT_DAY_START = 6  # earliest hour considered
COMFORT_DAY_END = 22  # latest hour considered (inclusive)
COMFORT_MIN_WINDOW_HOURS = 2  # skip blips shorter than this
# Golden hour: sun altitude span ~10° (extended definition, -4° to +6°). Sun's
# altitude near horizon changes at ~15° × cos(lat) per hour, so duration ≈
# 40 / cos(lat) min. Capped at 180 min for polar latitudes.
GOLDEN_HOUR_ARC_DEG = 10
GOLDEN_HOUR_MAX_MINUTES = 180

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


def _wmo_emoji(code: int | None) -> str:
    if code is None:
        return "🌡"
    return _WMO_EMOJI.get(int(code), "🌡")


def _uv_label(uv: float) -> str:
    if uv < 3:
        return "baixo"
    if uv < 6:
        return "moderado"
    if uv < 8:
        return "alto"
    if uv < 11:
        return "muito alto"
    return "extremo"


def _build_url(cfg: dict) -> str:
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


def _find_hourly_index(times: list[str], date_str: str, hour: int) -> int | None:
    target = f"{date_str}T{hour:02d}:00"
    try:
        return times.index(target)
    except ValueError:
        return None


def _uv_window(
    hourly: dict,
    today_str: str,
    uv_threshold: int,
) -> tuple[int | None, int | None, float]:
    times: list[str] = hourly.get("time", [])
    uvs: list = hourly.get("uv_index", [])
    start_h: int | None = None
    end_h: int | None = None
    peak = 0.0
    in_window = False
    for h in _DISPLAY_HOURS:
        idx = _find_hourly_index(times, today_str, h)
        if idx is None or idx >= len(uvs):
            continue
        uv = uvs[idx] or 0.0
        if uv >= uv_threshold:
            if not in_window:
                start_h = h
                in_window = True
            end_h = h
            if uv > peak:
                peak = uv
        else:
            if in_window:
                break  # first contiguous block only
    return start_h, end_h, peak


def _rain_window(
    hourly: dict,
    date_str: str,
    prob_threshold: int,
) -> tuple[int | None, int | None, float]:
    """First contiguous hourly block where precipitation_probability >= threshold."""
    times: list[str] = hourly.get("time", [])
    probs: list = hourly.get("precipitation_probability", [])
    start_h: int | None = None
    end_h: int | None = None
    peak = 0.0
    in_window = False
    for h in range(24):
        idx = _find_hourly_index(times, date_str, h)
        if idx is None or idx >= len(probs):
            continue
        p = probs[idx] or 0.0
        if p >= prob_threshold:
            if not in_window:
                start_h = h
                in_window = True
            end_h = h
            if p > peak:
                peak = p
        else:
            if in_window:
                break
    return start_h, end_h, peak


def _comfort_windows(hourly: dict, today_str: str) -> list[tuple[int, int]]:
    """Contiguous hour blocks where apparent temp is in comfort range AND rain risk is low.

    Scans COMFORT_DAY_START..COMFORT_DAY_END hour-by-hour. Returns list of
    (start_h, end_h) tuples (end inclusive). Blocks shorter than
    COMFORT_MIN_WINDOW_HOURS are skipped.
    """
    times: list[str] = hourly.get("time", [])
    feels: list = hourly.get("apparent_temperature", [])
    probs: list = hourly.get("precipitation_probability", [])

    windows: list[tuple[int, int]] = []
    start: int | None = None
    last: int | None = None

    def _flush() -> None:
        if start is not None and last is not None:
            if last - start + 1 >= COMFORT_MIN_WINDOW_HOURS:
                windows.append((start, last))

    for h in range(COMFORT_DAY_START, COMFORT_DAY_END + 1):
        idx = _find_hourly_index(times, today_str, h)
        if idx is None or idx >= len(feels):
            _flush()
            start = None
            last = None
            continue
        feel = feels[idx] or 0.0
        prob = (probs[idx] if idx < len(probs) else 0) or 0
        comfortable = COMFORT_TEMP_MIN <= feel <= COMFORT_TEMP_MAX and prob < RAIN_TODAY_PROB_MIN
        if comfortable:
            if start is None:
                start = h
            last = h
        else:
            _flush()
            start = None
            last = None

    _flush()
    return windows


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


def _daily_humidity_mean(hourly: dict, date_str: str) -> float | None:
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


def _format_today(
    day_idx: int,
    today_str: str,
    weekday: str,
    location_name: str,
    daily: dict,
    hourly: dict,
    *,
    uv_threshold: int,
) -> list[str]:
    def _d(key: str):
        vals = daily.get(key, [])
        return vals[day_idx] if day_idx < len(vals) else None

    feels_min = int(round(_d("apparent_temperature_min") or 0))
    feels_max = int(round(_d("apparent_temperature_max") or 0))
    precip_mm = int(round(_d("precipitation_sum") or 0))
    precip_prob = int(round(_d("precipitation_probability_max") or 0))
    uv_max = _d("uv_index_max") or 0.0
    wcode = _d("weather_code")
    wind_speed = int(round(_d("wind_speed_10m_max") or 0))
    wind_gust = int(round(_d("wind_gusts_10m_max") or 0))

    dd_mm = today_str[8:10] + "/" + today_str[5:7]
    lines = [
        f"### {_wmo_emoji(wcode)} {location_name} · {weekday} {dd_mm}",
        "",
        f"🌡 ↓{feels_min}°C  ↑{feels_max}°C   💧 {precip_mm}mm ({precip_prob}%)",
        f"💨 {wind_speed}km/h (raj. {wind_gust}km/h)",
    ]

    start_h, end_h, peak_uv = _uv_window(hourly, today_str, uv_threshold)
    if start_h is not None and end_h is not None:
        label = _uv_label(uv_max)
        lines.append(f"🔆 UV {label} {start_h}h–{end_h}h (pico {int(round(peak_uv))})")

    lines.append("")

    times: list[str] = hourly.get("time", [])
    h_feels = hourly.get("apparent_temperature", [])
    h_prob = hourly.get("precipitation_probability", [])

    entries = []
    for h in _DISPLAY_HOURS:
        idx = _find_hourly_index(times, today_str, h)
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
        date_str = times[idx]
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            weekday = _PT_DAYS[(dt.weekday() + 1) % 7]
        except ValueError:
            weekday = "?"
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


def _find_today_idx(daily: dict, tz: ZoneInfo) -> int:
    today = datetime.now(tz).date().isoformat()
    times: list[str] = daily.get("time", [])
    try:
        return times.index(today)
    except ValueError:
        return 0


def _format_message(data: dict, cfg: dict) -> str:
    tz = ZoneInfo(cfg["timezone"])
    daily = data["daily"]
    hourly = data["hourly"]
    uv_threshold = cfg.get("uv_warn_threshold", 6)
    forecast_days = cfg.get("forecast_days", 7)
    location_name = cfg.get("location_name", "?")

    day_idx = _find_today_idx(daily, tz)
    today_str = daily["time"][day_idx]

    try:
        dt = datetime.strptime(today_str, "%Y-%m-%d")
        weekday = _PT_DAYS[(dt.weekday() + 1) % 7]
    except ValueError:
        weekday = "?"

    lines = []
    lines += _format_today(
        day_idx, today_str, weekday, location_name, daily, hourly, uv_threshold=uv_threshold
    )
    lines += _format_forecast(day_idx, daily, forecast_days)
    return "\n".join(lines)


# --- Smart mode ---


def _format_smart_today(
    day_idx: int,
    today_str: str,
    weekday: str,
    location_name: str,
    daily: dict,
    hourly: dict,
    *,
    uv_threshold: int,
    latitude: float,
) -> list[str]:
    def _d(key: str):
        vals = daily.get(key, [])
        return vals[day_idx] if day_idx < len(vals) else None

    feels_min = int(round(_d("apparent_temperature_min") or 0))
    feels_max = int(round(_d("apparent_temperature_max") or 0))
    precip_mm = _d("precipitation_sum") or 0.0
    precip_prob = int(round(_d("precipitation_probability_max") or 0))
    uv_max = _d("uv_index_max") or 0.0
    wcode = _d("weather_code")

    dd_mm = today_str[8:10] + "/" + today_str[5:7]
    lines = [
        f"### {_wmo_emoji(wcode)} {location_name} · {weekday} {dd_mm}",
        "",
        f"🌡 sensação ↓{feels_min}°C  ↑{feels_max}°C",
    ]

    uv_start, uv_end, uv_peak = _uv_window(hourly, today_str, uv_threshold)
    if uv_start is not None and uv_end is not None:
        label = _uv_label(uv_max)
        lines.append(f"🔆 UV {label} {uv_start}h–{uv_end}h (pico {int(round(uv_peak))})")

    if precip_prob >= RAIN_TODAY_PROB_MIN and precip_mm >= RAIN_TODAY_MM_MIN:
        r_start, r_end, _peak = _rain_window(hourly, today_str, RAIN_TODAY_PROB_MIN)
        window_part = ""
        if r_start is not None and r_end is not None:
            window_part = f" {r_start}h–{r_end}h"
        lines.append(f"💧 {int(round(precip_mm))}mm {precip_prob}% chuva{window_part}")

    comfort = _comfort_windows(hourly, today_str)
    if comfort:
        win_str = ", ".join(f"{s}h–{e}h" for s, e in comfort)
        lines.append(f"😎 agradável {win_str}")

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
    d_app_max = daily.get("apparent_temperature_max", [])
    d_app_min = daily.get("apparent_temperature_min", [])
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
        hm = _daily_humidity_mean(hourly, times[i])
        if hm is not None:
            hum_vals.append(hm)
    if len(hum_vals) >= RECENT_MIN_SAMPLES:
        hum_pair = (statistics.fmean(hum_vals), statistics.stdev(hum_vals))
    else:
        hum_pair = None

    return {
        "apparent_max": _stats(d_app_max),
        "apparent_min": _stats(d_app_min),
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
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            weekday = _PT_DAYS[(dt.weekday() + 1) % 7]
        except ValueError:
            weekday = "?"

        parts: list[str] = []

        mm = d_precip[idx] if idx < len(d_precip) else 0.0
        prob = int(round(d_prob[idx] if idx < len(d_prob) else 0) or 0)
        if mm >= RAIN_NEXT_MM_MIN and prob >= RAIN_NEXT_PROB_MIN:
            r_start, r_end, _peak = _rain_window(hourly, date_str, RAIN_NEXT_PROB_MIN)
            window_part = ""
            if r_start is not None and r_end is not None:
                window_part = f" {r_start}h–{r_end}h"
            parts.append(f"💧 {int(round(mm))}mm {prob}%{window_part}")

        anomaly = _pick_apparent_anomaly(idx, d_app_max, d_app_min, hist_baseline, recent_baseline)
        if anomaly:
            parts.append(anomaly)

        day_hum = _daily_humidity_mean(hourly, date_str)
        if day_hum is not None:
            decision = _evaluate_anomaly(
                day_hum, hist_baseline.get("humidity"), recent_baseline.get("humidity")
            )
            if decision is not None:
                parts.append(_render_anomaly_humidity_line(day_hum, decision))

        if parts:
            lines.append(f"**{weekday}** " + "  ".join(parts))

    if not lines:
        return []
    return ["", "**Próximos dias**", *lines]


def _format_smart_message(data: dict, cfg: dict, normals: dict | None) -> str:
    tz = ZoneInfo(cfg["timezone"])
    daily = data["daily"]
    hourly = data["hourly"]
    uv_threshold = cfg.get("uv_warn_threshold", 6)
    forecast_days = cfg.get("forecast_days", 7)
    location_name = cfg.get("location_name", "?")

    day_idx = _find_today_idx(daily, tz)
    today_str = daily["time"][day_idx]

    try:
        dt = datetime.strptime(today_str, "%Y-%m-%d")
        weekday = _PT_DAYS[(dt.weekday() + 1) % 7]
    except ValueError:
        weekday = "?"

    lines: list[str] = []
    lines += _format_smart_today(
        day_idx,
        today_str,
        weekday,
        location_name,
        daily,
        hourly,
        uv_threshold=uv_threshold,
        latitude=cfg["latitude"],
    )
    lines += _format_smart_forecast(day_idx, daily, hourly, forecast_days, normals)
    return "\n".join(lines)


# --- Climate normals (Open-Meteo Archive API, ERA5 reanalysis) ---


def _climate_cache_fresh(cache: dict | None, now_local: datetime) -> bool:
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
    url = f"{ARCHIVE_URL}?{qs}"

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.error("[weather] Archive API returned HTTP %s", resp.status)
                return None
            data = await resp.json()
    except Exception as exc:
        log.error("[weather] Archive HTTP error: %s", exc)
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


# --- Source ---


class WeatherSource(Source):
    async def pull(
        self,
        cfg: dict,
        seen: set[str],
        session: aiohttp.ClientSession,
    ) -> PullResult | None:
        url = _build_url(cfg)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.error("[weather] Open-Meteo returned HTTP %s", resp.status)
                    return None
                data = await resp.json()
        except Exception as exc:
            log.error("[weather] HTTP error: %s", exc)
            return None

        if "daily" not in data or "hourly" not in data:
            log.error("[weather] Unexpected response shape: %s", list(data.keys()))
            return None

        try:
            if cfg.get("kind") == "smart":
                normals = cfg.get("_climate_normals")
                body = _format_smart_message(data, cfg, normals)
            else:
                body = _format_message(data, cfg)
        except Exception as exc:
            log.error("[weather] Format error: %s", exc)
            return None

        tz = ZoneInfo(cfg["timezone"])
        today_str = data["daily"]["time"][_find_today_idx(data["daily"], tz)]
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
