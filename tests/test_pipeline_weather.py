"""E2E tests for the weather pipeline (Open-Meteo → DiscordText)."""

import json
from datetime import date

import aiohttp

from pull.weather import _build_url
from tasks import _process_weather_task
from tests.conftest import WEBHOOK_URL

TIMEZONE = "America/Sao_Paulo"


def make_weather_cfg(
    *,
    name: str = "test-weather",
    uv_warn_threshold: int = 6,
    forecast_days: int = 3,
) -> dict:
    return {
        "name": name,
        "pull": [
            {
                "weather": {
                    "latitude": -19.52,
                    "longitude": -41.03,
                    "location_name": "Baixo Guandu",
                    "timezone": TIMEZONE,
                    "uv_warn_threshold": uv_warn_threshold,
                    "forecast_days": forecast_days,
                }
            }
        ],
        "push": [{"discord": {"webhook": WEBHOOK_URL}}],
    }


def make_payload(today_str: str, *, uv_at_noon: float = 9.0, forecast_days: int = 3) -> dict:
    """Build a minimal realistic Open-Meteo response."""
    days = [
        (date.fromisoformat(today_str) + __import__("datetime").timedelta(days=i)).isoformat()
        for i in range(forecast_days)
    ]
    # hourly: 24h * forecast_days entries
    hourly_times = [f"{d}T{h:02d}:00" for d in days for h in range(24)]
    n_hourly = len(hourly_times)

    def hourly_uv():
        vals = [0.0] * n_hourly
        # UV at 15h scales with noon so below-threshold tests stay below threshold
        uv_15h = uv_at_noon * 0.78
        for h, uv in [(12, uv_at_noon), (15, uv_15h)]:
            idx = hourly_times.index(f"{today_str}T{h:02d}:00")
            vals[idx] = uv
        return vals

    return {
        "daily": {
            "time": days,
            "apparent_temperature_max": [34.0] * forecast_days,
            "apparent_temperature_min": [22.0] * forecast_days,
            "precipitation_sum": [12.0] * forecast_days,
            "precipitation_probability_max": [85.0] * forecast_days,
            "uv_index_max": [uv_at_noon] * forecast_days,
            "weather_code": [63] * forecast_days,
            "wind_speed_10m_max": [45.0] * forecast_days,
            "wind_gusts_10m_max": [65.0] * forecast_days,
        },
        "hourly": {
            "time": hourly_times,
            "apparent_temperature": [27.0] * n_hourly,
            "precipitation_probability": [40.0] * n_hourly,
            "uv_index": hourly_uv(),
        },
    }


def _get_posted_body(mock_http) -> str:
    from yarl import URL

    calls = mock_http.requests.get(("POST", URL(WEBHOOK_URL)), [])
    assert calls, "No POST was made to the webhook"
    return json.loads(calls[0].kwargs["data"])["content"]


async def test_weather_happy_path(mock_http):
    today_str = date.today().isoformat()
    cfg = make_weather_cfg()
    weather_cfg = cfg["pull"][0]["weather"]
    url = _build_url(weather_cfg)

    mock_http.get(url, payload=make_payload(today_str), status=200)
    mock_http.post(WEBHOOK_URL, status=204)

    async with aiohttp.ClientSession() as session:
        result = await _process_weather_task(cfg, {}, session)

    assert "test-weather" in result
    assert result["test-weather"]["last_run"]

    body = _get_posted_body(mock_http)
    assert body.startswith("​\n")
    assert "### " in body
    assert "Baixo Guandu" in body
    assert "**Próximos dias**" in body
    assert "↓" in body and "↑" in body
    assert "°C" in body
    assert "⚠" not in body  # UV warn only shown on the window line, not per-hour
    assert "🔆 UV" in body
    assert "**11h**:" in body
    assert "💨" in body
    assert "·" in body


async def test_weather_api_failure(mock_http):
    cfg = make_weather_cfg()
    url = _build_url(cfg["pull"][0]["weather"])

    mock_http.get(url, status=500)

    async with aiohttp.ClientSession() as session:
        result = await _process_weather_task(cfg, {}, session)

    assert result == {}
    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 0


async def test_weather_post_failure(mock_http):
    """Discord 400 is logged but does not prevent state from being saved (matches search task behavior)."""
    today_str = date.today().isoformat()
    cfg = make_weather_cfg()
    url = _build_url(cfg["pull"][0]["weather"])

    mock_http.get(url, payload=make_payload(today_str), status=200)
    mock_http.post(WEBHOOK_URL, status=400)

    async with aiohttp.ClientSession() as session:
        result = await _process_weather_task(cfg, {}, session)

    # DiscordTextTarget.push() handles errors internally (logs, returns failed set, does not raise)
    # so the task saves last_run — consistent with how _process_research_task behaves
    assert "test-weather" in result


async def test_weather_uv_below_threshold(mock_http):
    today_str = date.today().isoformat()
    cfg = make_weather_cfg(uv_warn_threshold=6)
    url = _build_url(cfg["pull"][0]["weather"])

    mock_http.get(url, payload=make_payload(today_str, uv_at_noon=2.0), status=200)
    mock_http.post(WEBHOOK_URL, status=204)

    async with aiohttp.ClientSession() as session:
        await _process_weather_task(cfg, {}, session)

    body = _get_posted_body(mock_http)
    assert "🔆 UV" not in body
    assert "⚠" not in body


async def test_weather_analysis_dry_run(mock_http):
    today_str = date.today().isoformat()
    cfg = make_weather_cfg()
    url = _build_url(cfg["pull"][0]["weather"])

    mock_http.get(url, payload=make_payload(today_str), status=200)

    async with aiohttp.ClientSession() as session:
        result = await _process_weather_task(cfg, {}, session, analysis=True)

    assert result == {}
    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 0


async def test_weather_missing_hourly_slot(mock_http):
    today_str = date.today().isoformat()
    cfg = make_weather_cfg()
    url = _build_url(cfg["pull"][0]["weather"])

    payload = make_payload(today_str)
    # Remove the 9h slot from today's hourly data
    target = f"{today_str}T09:00"
    idx = payload["hourly"]["time"].index(target)
    for key in payload["hourly"]:
        payload["hourly"][key].pop(idx)

    mock_http.get(url, payload=payload, status=200)
    mock_http.post(WEBHOOK_URL, status=204)

    async with aiohttp.ClientSession() as session:
        await _process_weather_task(cfg, {}, session)

    body = _get_posted_body(mock_http)
    assert "**07h**:" in body
    assert "**09h**:" not in body
    assert "**11h**:" in body


async def test_weather_forecast_truncation(mock_http):
    """forecast_days=10 but API only returns 3 days — should not crash."""
    today_str = date.today().isoformat()
    cfg = make_weather_cfg(forecast_days=10)
    url = _build_url(cfg["pull"][0]["weather"])

    mock_http.get(url, payload=make_payload(today_str, forecast_days=3), status=200)
    mock_http.post(WEBHOOK_URL, status=204)

    async with aiohttp.ClientSession() as session:
        result = await _process_weather_task(cfg, {}, session)

    assert "test-weather" in result


async def test_weather_precip_hidden(mock_http):
    """Precipitation is hidden when both probability and volume are below threshold."""
    today_str = date.today().isoformat()
    cfg = make_weather_cfg()
    url = _build_url(cfg["pull"][0]["weather"])

    payload = make_payload(today_str)
    # Set precip below both thresholds in daily
    payload["daily"]["precipitation_sum"] = [0.0] * 3
    payload["daily"]["precipitation_probability_max"] = [5.0] * 3
    # Set hourly prob below threshold too
    n = len(payload["hourly"]["time"])
    payload["hourly"]["precipitation_probability"] = [5.0] * n

    mock_http.get(url, payload=payload, status=200)
    mock_http.post(WEBHOOK_URL, status=204)

    async with aiohttp.ClientSession() as session:
        await _process_weather_task(cfg, {}, session)

    body = _get_posted_body(mock_http)
    lines = body.split("\n")
    # Forecast is a single line after "**Próximos dias**"
    proximos_idx = next((i for i, line in enumerate(lines) if "**Próximos dias**" in line), None)
    assert proximos_idx is not None
    forecast_line = lines[proximos_idx + 1]
    assert "💧" not in forecast_line
    # Hourly entries are on a single joined line starting with **05h**
    hourly_line = next((line for line in lines if "**05h**" in line), None)
    assert hourly_line is not None
    assert "💧" not in hourly_line
