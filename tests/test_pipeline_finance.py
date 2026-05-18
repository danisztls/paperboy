"""E2E tests for the finance pipeline (yfinance → DiscordText).

yfinance is sync and doesn't go through aiohttp, so we monkeypatch the two
module-level fetcher functions (`_fetch_quote_with_history`, `_fetch_quote_fast`)
instead of using aioresponses.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest

import pull.finance as finance
from pull.finance import Quote, _infer_exchange, _is_market_open
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


@pytest.fixture
def market_open(monkeypatch):
    """Force monitor's market-state gate open so existing tests don't depend on wall clock."""
    monkeypatch.setattr(finance, "_is_market_open", lambda exchange, now_utc: True)


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


async def test_monitor_first_run_silent_records_baseline(monkeypatch, mock_http, market_open):
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


async def test_monitor_delta_fires_after_baseline(monkeypatch, mock_http, market_open):
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


async def test_monitor_delta_below_threshold_silent(monkeypatch, mock_http, market_open):
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


async def test_monitor_band_crossing_fires_once(monkeypatch, mock_http, market_open):
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


# --- Market-state gating unit tests ---


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("AAPL", "us_equity"),
        ("^GSPC", "us_equity"),
        ("ITSA4.SA", "b3"),
        ("EURUSD=X", "fx"),
        ("DX-Y.NYB", "fx"),
        ("BTC-USD", "crypto"),
        ("ETH-EUR", "crypto"),
        ("ABC-XYZ", "us_equity"),  # unknown quote ccy → not crypto, falls through
    ],
)
def test_infer_exchange(ticker, expected):
    assert _infer_exchange(ticker) == expected


def _utc(year, month, day, hour, minute, tz):
    """Build a UTC datetime from a wall-clock time in the given tz."""
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


@pytest.mark.parametrize(
    "exchange,when_local,tz,expected",
    [
        # us_equity: 09:30–16:00 ET, Mon–Fri
        ("us_equity", (2026, 5, 13, 10, 0), "America/New_York", True),  # Wed 10:00
        ("us_equity", (2026, 5, 13, 9, 29), "America/New_York", False),  # 1 min early
        ("us_equity", (2026, 5, 13, 16, 0), "America/New_York", False),  # exactly close
        ("us_equity", (2026, 5, 16, 12, 0), "America/New_York", False),  # Saturday
        # DST sanity — same wall-clock open in summer (EDT) and winter (EST)
        ("us_equity", (2026, 1, 14, 10, 0), "America/New_York", True),  # EST winter
        ("us_equity", (2026, 7, 15, 10, 0), "America/New_York", True),  # EDT summer
        # b3: 10:00–17:00 São Paulo, Mon–Fri
        ("b3", (2026, 5, 13, 11, 0), "America/Sao_Paulo", True),
        ("b3", (2026, 5, 13, 17, 0), "America/Sao_Paulo", False),
        ("b3", (2026, 5, 16, 11, 0), "America/Sao_Paulo", False),  # Saturday
        # fx: Sun 17:00 ET → Fri 17:00 ET
        ("fx", (2026, 5, 13, 3, 0), "America/New_York", True),  # Wed pre-dawn
        ("fx", (2026, 5, 15, 16, 59), "America/New_York", True),  # Fri 16:59 ET
        ("fx", (2026, 5, 15, 17, 0), "America/New_York", False),  # Fri close
        ("fx", (2026, 5, 16, 12, 0), "America/New_York", False),  # Saturday
        ("fx", (2026, 5, 17, 16, 0), "America/New_York", False),  # Sun pre-open
        ("fx", (2026, 5, 17, 17, 0), "America/New_York", True),  # Sun open
        # crypto: always
        ("crypto", (2026, 5, 16, 3, 0), "America/New_York", True),  # Sat 3am
    ],
)
def test_is_market_open(exchange, when_local, tz, expected):
    assert _is_market_open(exchange, _utc(*when_local, tz)) is expected


async def test_monitor_skips_closed_market(monkeypatch, mock_http):
    """Closed-market rules skip fetch + alerts entirely; their state is preserved."""
    fetch_calls: list[str] = []

    def _fake_fast(t):
        fetch_calls.append(t)
        return _quote(t, 100.00)

    monkeypatch.setattr(finance, "_fetch_quote_fast", _fake_fast)
    # Freeze the clock to a Saturday — us_equity closed, crypto open.
    saturday = _utc(2026, 5, 16, 12, 0, "America/New_York")
    monkeypatch.setattr(finance, "_now_utc", lambda: saturday)

    cfg = _monitor_cfg(
        rules=[
            {"ticker": "AAPL", "delta": 0.0001},  # inferred us_equity → skipped
            {"ticker": "BTC-USD", "delta": 0.0001},  # inferred crypto → fetched
        ]
    )
    state = {
        "tasks": {
            "test-finance-monitor": {
                "last_run": "2026-05-15T19:00:00+00:00",
                "tickers": {
                    "AAPL": {"last_price": 220.50},
                    "BTC-USD": {"last_price": 50000.00},
                },
            }
        }
    }
    async with aiohttp.ClientSession() as session:
        result = await _process_finance_task(cfg, state, session)

    # Only BTC-USD was fetched; AAPL skipped entirely.
    assert fetch_calls == ["BTC-USD"]
    # AAPL state preserved verbatim; BTC-USD rolled forward to new price.
    out = result["test-finance-monitor"]
    assert out["tickers"]["AAPL"] == {"last_price": 220.50}
    assert out["tickers"]["BTC-USD"]["last_price"] == 100.00
