# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Period-based due checks.

`period`'s suffix decides the comparison kind: `Nm`/`Nh` are sliding-window
durations, `Nd`/`Nw` are calendar-aligned (local date / ISO week) so morning
cron sweeps fire as soon as the date rolls over.
"""

import logging
from datetime import datetime, timedelta

from config import Period, get_feeds, parse_period, task_kind

DEFAULT_PERIOD = Period(count=1, unit="h")
PERIOD_GRACE = timedelta(seconds=60)

log = logging.getLogger(__name__)

# Kinds whose state stores a single task-level last_run (vs per-feed last_run).
_TASK_LEVEL_KINDS = ("research", "realestate", "weather", "finance")


def is_due(feed_state: dict, period: Period, now: datetime) -> bool:
    last_run = feed_state.get("last_run")
    if not last_run:
        return True
    try:
        last = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    if not period.is_calendar:
        return (now - last) >= period.as_timedelta() - PERIOD_GRACE
    last_local = last.astimezone().date()
    now_local = now.astimezone().date()
    if period.unit == "d":
        return (now_local - last_local).days >= period.count
    # period.unit == "w" — ISO week, Monday-anchored
    ly, lw, _ = last_local.isocalendar()
    ny, nw, _ = now_local.isocalendar()
    return (ny * 53 + nw) - (ly * 53 + lw) >= period.count


def _feed_period(fc: dict, task_period: Period) -> Period:
    """A feed's effective period: its own `period:` if set, else the task period."""
    raw = fc.get("period")
    return parse_period(raw) if raw else task_period


def due_feeds(
    feed_cfgs: list[dict], feeds_state: dict, task_period: Period, now: datetime
) -> list[dict]:
    """Feed cfgs whose own (or inherited task) period has elapsed."""
    return [
        fc
        for fc in feed_cfgs
        if is_due(feeds_state.get(fc["url"], {}), _feed_period(fc, task_period), now)
    ]


def task_is_due(task_cfg: dict, task_state: dict, period: Period, now: datetime) -> bool:
    """Whether a task should run this sweep.

    Task-level kinds compare the task's own last_run; feed tasks are due when
    any of their feeds is due at its own period (a transiently broken feed keeps
    its own clock). A task therefore wakes on the shortest of its feeds' periods.
    """
    if task_kind(task_cfg) in _TASK_LEVEL_KINDS:
        return is_due(task_state, period, now)
    feeds_state = task_state.get("feeds", {})
    feed_cfgs = [f for f in get_feeds(task_cfg) if f.get("url")]
    return bool(due_feeds(feed_cfgs, feeds_state, period, now))
