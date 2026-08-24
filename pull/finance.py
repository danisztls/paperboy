# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import aiohttp

from pipeline import Item, PullResult, Source

log = logging.getLogger(__name__)

Exchange = Literal["us_equity", "b3", "fx", "crypto"]


@dataclass
class Quote:
    ticker: str
    price: float
    week_ago_price: float | None
    year_low: float | None
    year_high: float | None


@dataclass
class MonitorRule:
    ticker: str
    delta: float
    price_band: tuple[float, float] | None
    exchange: Exchange  # inferred from ticker suffix or explicit override


@dataclass
class Alert:
    ticker: str
    current_price: float
    label: str  # e.g. "15min", "crossed" — rendered as the bold key
    value: str  # e.g. "↓ 5.4%", "$215.00 (band low)" — rendered as the value


# --- yfinance fetchers (sync; called via asyncio.to_thread). ---
# Module-level so tests can monkeypatch.


def _fetch_quote_with_history(ticker: str) -> Quote | None:
    """Pull 1y daily history; derive last close, week-ago close, 52w high/low."""
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        hist = t.history(period="1y", auto_adjust=True)
        if hist.empty:
            log.error("[finance] %s returned no history", ticker)
            return None
        price = float(hist["Close"].iloc[-1])
        # iloc[-6] = 5 trading days ago (≈ 1 trading week back from today's close)
        week_ago = float(hist["Close"].iloc[-6]) if len(hist) >= 6 else None
        year_high = float(hist["High"].max())
        year_low = float(hist["Low"].min())
        return Quote(
            ticker=ticker,
            price=price,
            week_ago_price=week_ago,
            year_low=year_low,
            year_high=year_high,
        )
    except Exception as exc:
        log.error("[finance] yfinance failed for %s: %s", ticker, exc)
        return None


def _fetch_quote_fast(ticker: str) -> Quote | None:
    """Last price only (no history) — for monitor mode where delta uses state baseline."""
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        info = t.fast_info
        price = info.last_price
        if price is None:
            log.error("[finance] %s has no last_price", ticker)
            return None
        return Quote(
            ticker=ticker,
            price=float(price),
            week_ago_price=None,
            year_low=None,
            year_high=None,
        )
    except Exception as exc:
        log.error("[finance] yfinance fast_info failed for %s: %s", ticker, exc)
        return None


async def _fetch_quotes(tickers: list[str], *, with_history: bool) -> dict[str, Quote | None]:
    async def _one(t: str) -> tuple[str, Quote | None]:
        # Re-lookup module attr each call so monkeypatch on the module takes effect.
        import pull.finance as _fin

        fn = _fin._fetch_quote_with_history if with_history else _fin._fetch_quote_fast
        return t, await asyncio.to_thread(fn, t)

    results = await asyncio.gather(*[_one(t) for t in tickers])
    return dict(results)


# --- Formatting helpers ---


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}"


def _fmt_pct_signed(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.1f}%"


def _fmt_pct_int(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{int(round(abs(v)))}%"


def _fmt_elapsed(minutes: int | None) -> str:
    if minutes is None or minutes <= 0:
        return "since last run"
    if minutes < 60:
        return f"{minutes}min"
    h, m = divmod(minutes, 60)
    if m == 0:
        return f"{h}h"
    return f"{h}h{m}min"


def _header_date(now_local: datetime, *, with_time: bool) -> str:
    month = now_local.strftime("%b")
    base = f"{month} {now_local.day}"
    if with_time:
        return f"{base} {now_local.strftime('%H:%M')}"
    return base


def _format_report(quotes: dict[str, Quote | None], now_local: datetime) -> str:
    lines = [f"## 📊 {_header_date(now_local, with_time=False)}"]

    for ticker, q in quotes.items():
        lines.append(f"### {ticker}")
        if q is None:
            lines.append("—")
            continue
        parts = [f"**now**: ${_fmt_price(q.price)}"]
        if q.week_ago_price is not None:
            wk_pct = (q.price - q.week_ago_price) / q.week_ago_price * 100
            parts.append(f"**week**: {_fmt_pct_signed(wk_pct)}")
        if q.year_low is not None and q.year_high is not None and q.price > 0:
            low_d = _fmt_pct_int((q.year_low - q.price) / q.price * 100)
            high_d = _fmt_pct_int((q.year_high - q.price) / q.price * 100)
            parts.append(
                f"**52w**: ${_fmt_price(q.year_low)} ({low_d})"
                f" – ${_fmt_price(q.year_high)} ({high_d})"
            )
        lines.append(" | ".join(parts))

    return "\n".join(lines)


def _format_monitor(alerts: list[Alert], now_local: datetime) -> str:
    lines = [f"## 🚨 {_header_date(now_local, with_time=True)}"]

    # Group by ticker so a single ticker firing multiple alerts (delta + level)
    # renders as one section with all parts joined by `|`.
    by_ticker: dict[str, list[Alert]] = {}
    for a in alerts:
        by_ticker.setdefault(a.ticker, []).append(a)

    for ticker, group in by_ticker.items():
        lines.append(f"### {ticker}")
        parts = [f"**now**: ${_fmt_price(group[0].current_price)}"]
        parts.extend(f"**{a.label}**: {a.value}" for a in group)
        lines.append(" | ".join(parts))

    return "\n".join(lines)


# --- Market schedule / gating ---


@dataclass(frozen=True)
class _Schedule:
    timezone: str
    open_hour: int
    open_minute: int
    close_hour: int
    close_minute: int
    weekdays: tuple[int, ...]  # 0=Mon, ..., 6=Sun


# Schedules in exchange local-clock wall time. ZoneInfo handles DST automatically.
# Holidays are intentionally ignored — false positives during half-days / holidays
# are rare and harmless (yfinance returns stale prices, no delta fires).
_SCHEDULES: dict[str, _Schedule] = {
    "us_equity": _Schedule("America/New_York", 9, 30, 16, 0, (0, 1, 2, 3, 4)),
    "b3": _Schedule("America/Sao_Paulo", 10, 0, 17, 0, (0, 1, 2, 3, 4)),
}

# Crypto-quote currency suffixes used by yfinance (e.g. BTC-USD, ETH-EUR).
_CRYPTO_QUOTES = {"USD", "EUR", "GBP", "JPY", "BRL", "BTC", "ETH"}


def _now_utc() -> datetime:
    """Indirection for tests to monkeypatch the wall clock."""
    return datetime.now(UTC)


def _infer_exchange(ticker: str) -> Exchange:
    """Best-effort exchange classification from the yfinance symbol shape.

    `.SA` → B3, `=X` → FX, `.NYB` → FX-like (ICE futures hours), `X-USD` style
    → crypto, anything else → US equity. Users can override per rule.
    """
    if ticker.endswith(".SA"):
        return "b3"
    if ticker.endswith("=X") or ticker.endswith(".NYB"):
        return "fx"
    if "-" in ticker:
        suffix = ticker.rsplit("-", 1)[-1]
        if suffix in _CRYPTO_QUOTES:
            return "crypto"
    return "us_equity"


def _is_market_open(exchange: Exchange, now_utc: datetime) -> bool:
    """Return True if the exchange is currently open. ZoneInfo handles DST."""
    if exchange == "crypto":
        return True
    if exchange == "fx":
        # Standard convention: open Sun 17:00 ET → Fri 17:00 ET.
        ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        wd = ny.weekday()  # Mon=0 … Sun=6
        if wd in (0, 1, 2, 3):  # Mon–Thu always open
            return True
        if wd == 4:  # Fri: open until 17:00 ET
            return ny.hour < 17
        if wd == 5:  # Sat always closed
            return False
        return ny.hour >= 17  # Sun: open from 17:00 ET
    sched = _SCHEDULES.get(exchange)
    if sched is None:
        return True  # unknown → fail open
    local = now_utc.astimezone(ZoneInfo(sched.timezone))
    if local.weekday() not in sched.weekdays:
        return False
    open_t = local.replace(hour=sched.open_hour, minute=sched.open_minute, second=0, microsecond=0)
    close_t = local.replace(
        hour=sched.close_hour, minute=sched.close_minute, second=0, microsecond=0
    )
    return open_t <= local < close_t


# --- Pull implementations ---


async def _pull_report(report_cfg: dict, task_name: str) -> PullResult | None:
    tickers = list(report_cfg.get("stocks") or [])
    if not tickers:
        return None
    quotes = await _fetch_quotes(tickers, with_history=True)
    if all(q is None for q in quotes.values()):
        log.error("[finance] All quotes failed for report task")
        return None
    now_local = datetime.now().astimezone()
    body = _format_report(quotes, now_local)
    item = Item(
        id=now_local.date().isoformat(),
        title=task_name,
        source="finance",
        body=body,
    )
    return PullResult(new_items=[item], current_items=[])


def _band_side(price: float, low: float, high: float) -> str:
    if price < low:
        return "below"
    if price > high:
        return "above"
    return "in"


async def _pull_monitor(
    rules_cfg: list[dict],
    state_tickers: dict,
    task_name: str,
    last_run: str | None,
) -> tuple[PullResult, dict]:
    """Returns (PullResult, new_tickers_state). PullResult.new_items is empty on no alerts."""
    rules = [
        MonitorRule(
            ticker=r["ticker"],
            delta=float(r["delta"]),
            price_band=tuple(r["price"]) if r.get("price") else None,
            exchange=r.get("exchange") or _infer_exchange(r["ticker"]),
        )
        for r in rules_cfg
    ]

    now = _now_utc()
    open_rules: list[MonitorRule] = []
    skipped_tickers: list[str] = []
    for rule in rules:
        if _is_market_open(rule.exchange, now):
            open_rules.append(rule)
        else:
            skipped_tickers.append(rule.ticker)
    if skipped_tickers:
        log.debug("[finance] Skipping closed-market tickers: %s", ", ".join(skipped_tickers))

    quotes = await _fetch_quotes([r.ticker for r in open_rules], with_history=False)

    elapsed_min: int | None = None
    if last_run:
        try:
            last = datetime.fromisoformat(last_run)
            elapsed_min = max(0, int(round((now - last).total_seconds() / 60)))
        except ValueError:
            pass

    alerts: list[Alert] = []
    new_tickers: dict[str, dict] = {}

    # Preserve state for tickers we skipped — they need their baseline for the
    # next open-market tick.
    for rule in rules:
        if rule.ticker in skipped_tickers and rule.ticker in state_tickers:
            new_tickers[rule.ticker] = state_tickers[rule.ticker]

    for rule in open_rules:
        q = quotes.get(rule.ticker)
        if q is None:
            # Preserve previous state on fetch failure
            if rule.ticker in state_tickers:
                new_tickers[rule.ticker] = state_tickers[rule.ticker]
            continue

        prev = state_tickers.get(rule.ticker, {})
        last_price = prev.get("last_price")
        prev_band_side = prev.get("band_side")

        # Delta alert vs previous tick's price
        if last_price is not None and last_price > 0:
            pct = (q.price - last_price) / last_price
            if abs(pct) >= rule.delta:
                arrow = "↑" if pct > 0 else "↓"
                alerts.append(
                    Alert(
                        ticker=rule.ticker,
                        current_price=q.price,
                        label=_fmt_elapsed(elapsed_min),
                        value=f"{arrow} {abs(pct) * 100:.1f}%",
                    )
                )

        # Band crossing
        entry: dict = {"last_price": q.price}
        if rule.price_band is not None:
            low, high = rule.price_band
            new_side = _band_side(q.price, low, high)
            if prev_band_side is not None and prev_band_side != new_side:
                if new_side == "below":
                    value = f"${_fmt_price(low)} (band low)"
                elif new_side == "above":
                    value = f"${_fmt_price(high)} (band high)"
                else:
                    boundary = low if prev_band_side == "below" else high
                    value = f"${_fmt_price(boundary)} (back in band)"
                alerts.append(
                    Alert(
                        ticker=rule.ticker,
                        current_price=q.price,
                        label="crossed",
                        value=value,
                    )
                )
            entry["band_side"] = new_side

        new_tickers[rule.ticker] = entry

    new_items: list[Item] = []
    if alerts:
        now_local = datetime.now().astimezone()
        body = _format_monitor(alerts, now_local)
        new_items = [
            Item(
                id=now.replace(microsecond=0).isoformat(),
                title=task_name,
                source="finance-monitor",
                body=body,
            )
        ]

    return PullResult(new_items=new_items, current_items=[]), new_tickers


# --- Source ---


class FinanceSource(Source):
    """Two modes via cfg sub-key: `report` (one-way pull) or `monitor` (bidirectional state).

    Monitor mode threads state through cfg: `_state_tickers` (input) and
    `_new_state_tickers` (output set as a side-effect on cfg, read by tasks.py).
    """

    async def pull(
        self,
        cfg: dict,
        seen: set[str],
        session: aiohttp.ClientSession,
    ) -> PullResult | None:
        task_name = cfg.get("_task_name", "finance")
        if "report" in cfg:
            return await _pull_report(cfg["report"], task_name)
        if "monitor" in cfg:
            state_tickers = cfg.get("_state_tickers", {})
            last_run = cfg.get("_last_run")
            result, new_tickers = await _pull_monitor(
                cfg["monitor"], state_tickers, task_name, last_run
            )
            cfg["_new_state_tickers"] = new_tickers
            return result
        log.error("[finance] cfg has neither 'report' nor 'monitor'")
        return None
