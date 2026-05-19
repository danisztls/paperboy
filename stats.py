"""Rich-rendered `--stats` view of state.json."""

from datetime import UTC, datetime, timedelta

from config import Period, get_feeds, parse_period, task_kind
from tasks import DEFAULT_PERIOD

_KIND_STYLES = {
    "feeds": "cyan",
    "digest": "bright_magenta",
    "scraper": "blue",
    "search": "yellow",
    "weather": "bright_green",
}


def _humanize_minutes(mins: int) -> str:
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        h, m = divmod(mins, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(mins, 1440)
    h = rem // 60
    return f"{d}d {h}h" if h else f"{d}d"


def _humanize_delta(seconds: float) -> str:
    """Signed humanized duration: `in 40m`, `3h 20m ago`, `now` for <30s."""
    if abs(seconds) < 30:
        return "now"
    mins = max(1, int(abs(seconds) // 60))
    label = _humanize_minutes(mins)
    return f"in {label}" if seconds > 0 else f"{label} ago"


def _next_run(last_iso: str | None, period: Period) -> datetime | None:
    """UTC datetime at which a task with `last_iso` becomes due, or None."""
    if not last_iso:
        return None
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return None
    if not period.is_calendar:
        return last + period.as_timedelta()
    last_local = last.astimezone()
    if period.unit == "d":
        next_local_date = last_local.date() + timedelta(days=int(period.count))
    else:  # period.unit == "w"
        last_monday = last_local.date() - timedelta(days=last_local.weekday())
        next_local_date = last_monday + timedelta(weeks=int(period.count))
    next_local = datetime.combine(next_local_date, datetime.min.time(), tzinfo=last_local.tzinfo)
    return next_local.astimezone(UTC)


def _format_last(last_iso: str | None, now: datetime) -> str:
    if not last_iso:
        return "[red dim]never[/red dim]"
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return "[red]invalid[/red]"
    return _humanize_delta(-(now - last).total_seconds())


def _format_next_from_dt(nxt: datetime | None, now: datetime) -> str:
    if nxt is None or nxt <= now:
        return "[green]due now[/green]"
    secs = (nxt - now).total_seconds()
    text = _humanize_delta(secs)
    if secs < 1800:  # <30 min
        return f"[yellow]{text}[/yellow]"
    return text


def _format_next(last_iso: str | None, period: Period, now: datetime) -> str:
    return _format_next_from_dt(_next_run(last_iso, period), now)


def _format_count(items: list[dict], curated: bool) -> str:
    total = len(items)
    if curated:
        passed = sum(1 for it in items if it.get("filter_pass") is True)
        if total == 0:
            return "[dim]0/0[/dim]"
        passed_style = "green" if passed > 0 else "dim"
        return f"[{passed_style}]{passed}[/{passed_style}][dim]/{total}[/dim]"
    if total == 0:
        return "[dim]0[/dim]"
    return str(total)


def _format_scalar(n: int) -> str:
    return "[dim]0[/dim]" if n == 0 else str(n)


def _format_kind(kind: str) -> str:
    style = _KIND_STYLES.get(kind, "white")
    return f"[{style}]{kind}[/{style}]"


def print_stats(config: dict, state: dict) -> None:
    """Render a Rich table of per-task and per-source state stats."""
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    now = datetime.now(UTC)
    tasks_state = state.get("tasks", {}) or {}
    tasks_cfg = config.get("tasks", []) or []

    table = Table(
        title="[bold]claudinho stats[/bold]",
        box=box.SIMPLE_HEAVY,
        header_style="bold",
        expand=False,
    )
    table.add_column("Task / Source", overflow="fold")
    table.add_column("Kind")
    table.add_column("Period", style="dim")
    table.add_column("Last run")
    table.add_column("Next run")
    table.add_column("Items", justify="right")

    known_task_names: set[str] = set()
    known_feeds_by_task: dict[str, set[str]] = {}
    known_adapters_by_task: dict[str, set[str]] = {}

    for task_cfg in tasks_cfg:
        name = task_cfg.get("name")
        if not name:
            continue
        known_task_names.add(name)
        kind = task_kind(task_cfg)
        period = parse_period(task_cfg.get("period", DEFAULT_PERIOD))
        ts = tasks_state.get(name, {}) or {}
        curated = bool(task_cfg.get("curate"))
        name_cell = f"[bold]{escape(name)}[/bold]"

        if kind in ("feeds", "digest"):
            feed_cfgs = get_feeds(task_cfg)
            feeds_state = ts.get("feeds", {}) or {}
            known_feeds_by_task[name] = {fc["url"] for fc in feed_cfgs if fc.get("url")}

            last_runs: list[str] = []
            next_runs: list[datetime] = []
            total = 0
            passed = 0
            any_no_last = False
            for fc in feed_cfgs:
                url = fc.get("url")
                if not url:
                    continue
                fs = feeds_state.get(url, {}) or {}
                lr = fs.get("last_run")
                if lr:
                    last_runs.append(lr)
                else:
                    any_no_last = True
                nr = _next_run(lr, period)
                if nr is None:
                    any_no_last = True
                else:
                    next_runs.append(nr)
                items = fs.get("items", []) or []
                total += len(items)
                if curated:
                    passed += sum(1 for it in items if it.get("filter_pass") is True)

            task_last = max(last_runs) if last_runs else None
            if any_no_last or not next_runs:
                task_next_str = "[green]due now[/green]"
            else:
                task_next_str = _format_next_from_dt(min(next_runs), now)
            if curated:
                if total == 0:
                    task_items = "[dim]0/0[/dim]"
                else:
                    p_style = "bold green" if passed > 0 else "dim"
                    task_items = f"[{p_style}]{passed}[/{p_style}][dim]/{total}[/dim]"
            else:
                task_items = _format_scalar(total)

            table.add_row(
                name_cell,
                _format_kind(kind),
                str(period),
                _format_last(task_last, now),
                task_next_str,
                task_items,
            )

            def _sort_key(fc: dict) -> tuple[str, str]:
                url = fc.get("url", "")
                fs = feeds_state.get(url, {}) or {}
                return (fs.get("name") or fc.get("name") or url, url)

            for fc in sorted(feed_cfgs, key=_sort_key):
                url = fc.get("url")
                if not url:
                    continue
                fs = feeds_state.get(url, {}) or {}
                display = fs.get("name") or fc.get("name") or url
                items = fs.get("items", []) or []
                table.add_row(
                    f"  [dim]↳[/dim] [dim]{escape(display)}[/dim]",
                    "",
                    "",
                    "",
                    "",
                    _format_count(items, curated),
                )
        elif kind == "scraper":
            scrapers_state = ts.get("scrapers", {}) or {}
            scraper_cfgs = [
                item["scraper"] for item in task_cfg.get("pull", []) if "scraper" in item
            ]
            known_adapters_by_task[name] = {sc.get("adapter") for sc in scraper_cfgs}

            last_runs: list[str] = []
            next_runs: list[datetime] = []
            total = 0
            any_no_last = False
            for sc in scraper_cfgs:
                adapter = sc.get("adapter")
                ss = scrapers_state.get(adapter, {}) or {}
                lr = ss.get("last_run")
                if lr:
                    last_runs.append(lr)
                else:
                    any_no_last = True
                nr = _next_run(lr, period)
                if nr is None:
                    any_no_last = True
                else:
                    next_runs.append(nr)
                total += len(ss.get("items", []) or [])

            task_last = max(last_runs) if last_runs else ts.get("last_run")
            if any_no_last or not next_runs:
                task_next_str = "[green]due now[/green]"
            else:
                task_next_str = _format_next_from_dt(min(next_runs), now)

            table.add_row(
                name_cell,
                _format_kind(kind),
                str(period),
                _format_last(task_last, now),
                task_next_str,
                _format_scalar(total),
            )
            for sc in sorted(scraper_cfgs, key=lambda s: s.get("adapter", "")):
                adapter = sc.get("adapter") or "?"
                ss = scrapers_state.get(adapter, {}) or {}
                items = ss.get("items", []) or []
                table.add_row(
                    f"  [dim]↳[/dim] [dim]{escape(adapter)}[/dim]",
                    "",
                    "",
                    "",
                    "",
                    _format_scalar(len(items)),
                )
        else:  # search, weather, or any other task-level-only kind
            table.add_row(
                name_cell,
                _format_kind(kind),
                str(period),
                _format_last(ts.get("last_run"), now),
                _format_next(ts.get("last_run"), period, now),
                "[dim]—[/dim]",
            )

    console.print(table)

    # Stale state entries: tasks/feeds in state but not in config.
    stale_lines: list[str] = []
    for tname, ts in tasks_state.items():
        if tname in known_task_names:
            feeds_state = (ts or {}).get("feeds", {}) or {}
            known_urls = known_feeds_by_task.get(tname, set())
            for url in feeds_state:
                if url not in known_urls:
                    fname = (feeds_state[url] or {}).get("name") or url
                    stale_lines.append(
                        f"  [yellow]\\[{escape(tname)}][/yellow] [dim]{escape(fname)}[/dim]"
                    )
            scrapers_state = (ts or {}).get("scrapers", {}) or {}
            known_adapters = known_adapters_by_task.get(tname, set())
            for adapter in scrapers_state:
                if adapter == "__legacy__" or adapter in known_adapters:
                    continue
                stale_lines.append(
                    f"  [yellow]\\[{escape(tname)}][/yellow] [dim]{escape(adapter)}[/dim]"
                )
        else:
            stale_lines.append(f"  [yellow]{escape(tname)}[/yellow]")
    if stale_lines:
        console.print()
        console.print("[yellow]Stale state entries[/yellow] [dim](run --clean to remove):[/dim]")
        for line in stale_lines:
            console.print(line)
