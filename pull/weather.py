from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from pipeline import Item, PullResult, Source

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

_DAILY_VARS = ",".join(
    [
        "apparent_temperature_max",
        "apparent_temperature_min",
        "apparent_temperature_mean",
        "precipitation_sum",
        "precipitation_probability_max",
        "uv_index_max",
        "weather_code",
    ]
)

_HOURLY_VARS = ",".join(
    [
        "apparent_temperature",
        "precipitation_probability",
        "uv_index",
    ]
)

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
        ("forecast_days", cfg.get("forecast_days", 5)),
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

    dd_mm = today_str[8:10] + "/" + today_str[5:7]
    lines = [
        f"### {_wmo_emoji(wcode)} {location_name} · {weekday} {dd_mm}",
        "",
        f"🌡 ↓{feels_min}°C  ↑{feels_max}°C   💧 {precip_mm}mm ({precip_prob}%)",
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
        entries.append(f"**{h:02d}h**: {feels}°C 💧{prob}%")
    if entries:
        lines.append("  ·  ".join(entries))

    return lines


def _format_forecast(start_idx: int, daily: dict, forecast_days: int) -> list[str]:
    times: list[str] = daily.get("time", [])
    d_feels_mean = daily.get("apparent_temperature_mean", [])
    d_precip = daily.get("precipitation_sum", [])
    d_prob = daily.get("precipitation_probability_max", [])
    d_wcode = daily.get("weather_code", [])

    lines = ["", "**Próximos dias**"]
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
        dd = date_str[8:10]
        feels_avg = int(round(d_feels_mean[idx] if idx < len(d_feels_mean) else 0) or 0)
        mm = int(round(d_precip[idx] if idx < len(d_precip) else 0) or 0)
        prob = int(round(d_prob[idx] if idx < len(d_prob) else 0) or 0)
        wc = d_wcode[idx] if idx < len(d_wcode) else None
        lines.append(f"{weekday} {dd}  {_wmo_emoji(wc)}  avg {feels_avg}°C   {mm:2d}mm  {prob:2d}%")
    return lines


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
    forecast_days = cfg.get("forecast_days", 5)
    location_name = cfg.get("location_name", "?")

    day_idx = _find_today_idx(daily, tz)
    today_str = daily["time"][day_idx]

    try:
        dt = datetime.strptime(today_str, "%Y-%m-%d")
        weekday = _PT_DAYS[(dt.weekday() + 1) % 7]
    except ValueError:
        weekday = "?"

    # leading "" produces a leading \n when joined
    lines = [""]
    lines += _format_today(
        day_idx, today_str, weekday, location_name, daily, hourly, uv_threshold=uv_threshold
    )
    lines += _format_forecast(day_idx, daily, forecast_days)
    return "\n".join(lines)


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
