"""E2E tests for the finance pipeline (yfinance → DiscordText).

yfinance is sync and doesn't go through aiohttp, so we monkeypatch the two
module-level fetcher functions (`_fetch_quote_with_history`, `_fetch_quote_fast`)
instead of using aioresponses.
"""

import json

import aiohttp

import pull.finance as finance
from pull.finance import Quote
from tasks import _process_finance_task
from tests.conftest import WEBHOOK_URL


def _quote(ticker, price, *, week_ago=None, low=None, high=None):
    return Quote(
        ticker=ticker,
        price=price,
        week_ago_price=week_ago,
        year_low=low,
        year_high=high,
    )


def _patch_history(monkeypatch, quotes_by_ticker):
    monkeypatch.setattr(finance, "_fetch_quote_with_history", lambda t: quotes_by_ticker.get(t))


def _patch_fast(monkeypatch, quotes_by_ticker):
    monkeypatch.setattr(finance, "_fetch_quote_fast", lambda t: quotes_by_ticker.get(t))


def _report_cfg(name="test-finance-report", stocks=None):
    return {
        "name": name,
        "pull": [{"finance": {"report": {"stocks": stocks or ["AAPL"]}}}],
        "push": [{"discord": {"webhook": WEBHOOK_URL, "wrap": False}}],
    }


def _monitor_cfg(name="test-finance-monitor", rules=None):
    return {
        "name": name,
        "pull": [{"finance": {"monitor": rules or []}}],
        "push": [{"discord": {"webhook": WEBHOOK_URL, "wrap": False}}],
    }


def _get_posted_body(mock_http) -> str:
    from yarl import URL

    calls = mock_http.requests.get(("POST", URL(WEBHOOK_URL)), [])
    assert calls, "No POST was made to the webhook"
    return json.loads(calls[0].kwargs["data"])["content"]


async def test_report_happy_path(monkeypatch, mock_http):
    _patch_history(
        monkeypatch,
        {
            "AAPL": _quote("AAPL", 220.50, week_ago=218.0, low=165.0, high=240.0),
            "MSFT": _quote("MSFT", 440.20, week_ago=436.7, low=370.0, high=470.0),
        },
    )
    mock_http.post(WEBHOOK_URL, status=204)

    cfg = _report_cfg(stocks=["AAPL", "MSFT"])
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, {}, session)

    assert "test-finance-report" in result
    assert result["test-finance-report"]["last_run"]
    assert "tickers" not in result["test-finance-report"]  # report stores no per-ticker state

    body = _get_posted_body(mock_http)
    assert body.startswith("​\n")
    assert "## 📊 " in body  # H2 header is just emoji + date
    assert "Report" not in body  # literal "Report" removed
    assert "### AAPL" in body and "**now**: $220.50" in body
    assert "### MSFT" in body and "**now**: $440.20" in body
    assert " | " in body
    assert "**52w**: $165.00 (−25%)" in body and "$240.00 (+9%)" in body
    assert "_(" not in body  # deltas no longer italicized
    assert "```" not in body  # not code-block-wrapped
    # Week-change: AAPL = (220.5-218)/218 ≈ +1.1%
    assert "**week**: +1.1%" in body
    # No blank line between H3 and data line, nor between tickers
    assert "\n\n###" not in body


async def test_report_missing_data_renders_dash(monkeypatch, mock_http):
    _patch_history(
        monkeypatch,
        {
            "AAPL": _quote("AAPL", 220.50, week_ago=218.0, low=165.0, high=240.0),
            "BROKEN": None,  # simulated fetch failure
        },
    )
    mock_http.post(WEBHOOK_URL, status=204)

    cfg = _report_cfg(stocks=["AAPL", "BROKEN"])
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, {}, session)

    assert "test-finance-report" in result
    body = _get_posted_body(mock_http)
    assert "BROKEN" in body
    assert "—" in body  # em-dash for missing values


async def test_report_all_fetches_failed_no_post(monkeypatch, mock_http):
    _patch_history(monkeypatch, {"AAPL": None, "MSFT": None})
    mock_http.post(WEBHOOK_URL, status=204)

    cfg = _report_cfg(stocks=["AAPL", "MSFT"])
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, {}, session)

    assert result == {}
    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 0


async def test_monitor_first_run_silent_records_baseline(monkeypatch, mock_http):
    _patch_fast(
        monkeypatch,
        {
            "AAPL": _quote("AAPL", 220.50),
            "NVDA": _quote("NVDA", 900.00),
        },
    )

    cfg = _monitor_cfg(
        rules=[
            {"ticker": "AAPL", "delta": 0.05},
            {"ticker": "NVDA", "delta": 0.05, "price": [800.0, 950.0]},
        ]
    )
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, {}, session)

    # First run: no previous baseline → no alerts, state populated, no Discord post.
    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 0

    out = result["test-finance-monitor"]
    assert out["tickers"]["AAPL"]["last_price"] == 220.50
    assert out["tickers"]["NVDA"]["last_price"] == 900.00
    assert out["tickers"]["NVDA"]["band_side"] == "in"
    assert "band_side" not in out["tickers"]["AAPL"]  # no band on AAPL rule


async def test_monitor_delta_fires_after_baseline(monkeypatch, mock_http):
    _patch_fast(monkeypatch, {"AAPL": _quote("AAPL", 208.50)})  # 5.4% drop from 220.50
    mock_http.post(WEBHOOK_URL, status=204)

    cfg = _monitor_cfg(rules=[{"ticker": "AAPL", "delta": 0.05}])
    state = {
        "tasks": {
            "test-finance-monitor": {
                "last_run": "2026-05-17T14:17:00+00:00",
                "tickers": {"AAPL": {"last_price": 220.50}},
            }
        }
    }
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, state, session)

    body = _get_posted_body(mock_http)
    assert "## 🚨 " in body  # H2 with emoji + date — no task name
    assert "test-finance-monitor" not in body
    assert "### AAPL" in body
    assert "**now**: $208.50" in body
    assert "↓" in body and "5.4%" in body
    assert "```" not in body
    # State is updated to new baseline
    assert result["test-finance-monitor"]["tickers"]["AAPL"]["last_price"] == 208.50


async def test_monitor_delta_below_threshold_silent(monkeypatch, mock_http):
    _patch_fast(monkeypatch, {"AAPL": _quote("AAPL", 222.00)})  # 0.68% up — under 5%

    cfg = _monitor_cfg(rules=[{"ticker": "AAPL", "delta": 0.05}])
    state = {
        "tasks": {
            "test-finance-monitor": {
                "last_run": "2026-05-17T14:17:00+00:00",
                "tickers": {"AAPL": {"last_price": 220.50}},
            }
        }
    }
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, state, session)

    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 0
    # Baseline still rolls forward
    assert result["test-finance-monitor"]["tickers"]["AAPL"]["last_price"] == 222.00


async def test_monitor_band_crossing_fires_once(monkeypatch, mock_http):
    """Crossing out of band fires; staying outside on subsequent ticks doesn't re-fire."""
    cfg = _monitor_cfg(rules=[{"ticker": "NVDA", "delta": 0.50, "price": [800.0, 950.0]}])

    # Tick 1: was in-band at 900, now above 950 — fires "crossed 950 (band high)"
    _patch_fast(monkeypatch, {"NVDA": _quote("NVDA", 955.20)})
    mock_http.post(WEBHOOK_URL, status=204, repeat=True)

    state = {
        "tasks": {
            "test-finance-monitor": {
                "last_run": "2026-05-17T14:17:00+00:00",
                "tickers": {"NVDA": {"last_price": 900.00, "band_side": "in"}},
            }
        }
    }
    async with aiohttp.ClientSession() as session:
        r1 = await _process_finance_task(cfg, state, session)

    body1 = _get_posted_body(mock_http)
    assert "### NVDA" in body1
    assert "**now**: $955.20" in body1
    assert "**crossed**: $950.00 (band high)" in body1
    assert r1["test-finance-monitor"]["tickers"]["NVDA"]["band_side"] == "above"

    # Tick 2: still above 950 — must not re-fire
    _patch_fast(monkeypatch, {"NVDA": _quote("NVDA", 960.10)})
    state2 = {"tasks": {"test-finance-monitor": r1["test-finance-monitor"]}}
    async with aiohttp.ClientSession() as session:
        r2 = await _process_finance_task(cfg, state2, session)

    # Only one POST happened total
    posts = [c for c in mock_http.requests if c[0] == "POST"]
    assert len(posts) == 1
    assert r2["test-finance-monitor"]["tickers"]["NVDA"]["band_side"] == "above"
