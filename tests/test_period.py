"""Period parsing + is_due semantics across duration and calendar units."""

from datetime import UTC, datetime, timedelta

import pytest

from config import Period, parse_period
from tasks import is_due

# --- parse_period ---


def test_parse_period_duration_units():
    assert parse_period("30m") == Period(30, "m")
    assert parse_period("6h") == Period(6, "h")
    assert parse_period("0.5h") == Period(0.5, "h")


def test_parse_period_calendar_units():
    assert parse_period("1d") == Period(1, "d")
    assert parse_period("2d") == Period(2, "d")
    assert parse_period("1w") == Period(1, "w")


def test_parse_period_rejects_fractional_calendar():
    with pytest.raises(ValueError, match="must be a positive integer"):
        parse_period("1.5d")
    with pytest.raises(ValueError, match="must be a positive integer"):
        parse_period("0.5w")


def test_parse_period_rejects_missing_suffix():
    with pytest.raises(ValueError, match="missing suffix"):
        parse_period("30")


def test_parse_period_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        parse_period("")


def test_period_str_round_trips():
    for s in ("30m", "6h", "1d", "2d", "1w"):
        assert str(parse_period(s)) == s


def test_period_is_calendar():
    assert parse_period("1d").is_calendar
    assert parse_period("1w").is_calendar
    assert not parse_period("30m").is_calendar
    assert not parse_period("6h").is_calendar


def test_period_as_timedelta_only_for_duration():
    assert parse_period("6h").as_timedelta() == timedelta(hours=6)
    with pytest.raises(ValueError):
        parse_period("1d").as_timedelta()


# --- is_due: duration units (unchanged behavior) ---


def testis_due_duration_no_last_run():
    assert is_due({}, Period(30, "m"), datetime.now(UTC))


def testis_due_duration_not_yet_due():
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    last = (now - timedelta(minutes=10)).isoformat()
    assert not is_due({"last_run": last}, Period(30, "m"), now)


def testis_due_durationis_due():
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    last = (now - timedelta(minutes=31)).isoformat()
    assert is_due({"last_run": last}, Period(30, "m"), now)


def testis_due_duration_grace_window():
    # 30m period, 29min59s elapsed — within the 60s grace, should fire
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    last = (now - timedelta(seconds=29 * 60 + 59)).isoformat()
    assert is_due({"last_run": last}, Period(30, "m"), now)


# --- is_due: calendar day ---


def testis_due_calendar_day_same_local_date_not_due():
    # last run earlier today (local) — same calendar day, not due
    now = datetime(2026, 5, 17, 23, 0, tzinfo=UTC).astimezone()
    last = (now - timedelta(hours=6)).isoformat()
    assert not is_due({"last_run": last}, Period(1, "d"), now)


def testis_due_calendar_day_yesterdayis_due():
    now = datetime(2026, 5, 17, 6, 0, tzinfo=UTC).astimezone()
    last = (now - timedelta(days=1)).isoformat()
    assert is_due({"last_run": last}, Period(1, "d"), now)


def testis_due_calendar_day_just_after_midnightis_due():
    # 30 minutes after local midnight — even though wall-clock elapsed < 24h,
    # the local date has advanced, so 1d fires.
    now_local = datetime(2026, 5, 17, 0, 30).astimezone()
    last_local = datetime(2026, 5, 16, 23, 30).astimezone()
    assert is_due({"last_run": last_local.isoformat()}, Period(1, "d"), now_local)


def testis_due_calendar_2d_one_day_gap_not_due():
    now = datetime(2026, 5, 17, 6, 0, tzinfo=UTC).astimezone()
    last = (now - timedelta(days=1)).isoformat()
    assert not is_due({"last_run": last}, Period(2, "d"), now)


def testis_due_calendar_2d_two_day_gapis_due():
    now = datetime(2026, 5, 17, 6, 0, tzinfo=UTC).astimezone()
    last = (now - timedelta(days=2)).isoformat()
    assert is_due({"last_run": last}, Period(2, "d"), now)


# --- is_due: calendar week (ISO, Monday-anchored) ---


def testis_due_calendar_week_same_iso_week_not_due():
    # 2026-05-17 is a Sunday; 2026-05-11 is the Monday of that ISO week.
    # last run on Monday, now on Sunday — same week, not due.
    now = datetime(2026, 5, 17, 6, 0, tzinfo=UTC).astimezone()
    last = datetime(2026, 5, 11, 6, 0, tzinfo=UTC).isoformat()
    assert not is_due({"last_run": last}, Period(1, "w"), now)


def testis_due_calendar_week_previous_weekis_due():
    # last run in the previous ISO week — due.
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC).astimezone()  # Monday of next week
    last = datetime(2026, 5, 17, 23, 0, tzinfo=UTC).isoformat()  # Sunday of prior week
    assert is_due({"last_run": last}, Period(1, "w"), now)
