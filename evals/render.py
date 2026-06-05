"""Rich-rendered views of captured run data. Imported lazily from capture.RunCapture.display."""

from evals.capture import FeedRecord, LLMCall, TaskCapture


def render_run(tasks: list[TaskCapture], *, console=None) -> None:
    from rich.console import Console

    if console is None:
        console = Console()
    for task in tasks:
        _render_task(console, task)
    console.print()


def _render_task(console, task: TaskCapture) -> None:
    from rich.markup import escape

    console.print()
    console.rule(
        f"[bold cyan]{escape(task.task)}[/bold cyan]  "
        f"[dim]{escape(task.type)} · {escape(task.timestamp)}[/dim]"
    )
    for feed in task.feeds:
        _render_feed(console, feed)
    for call in task.calls:
        if call.call_type == "summarize":
            _render_summarize(console, call)
        elif call.call_type == "filter":
            _render_filter(console, call)
        elif call.call_type == "research":
            _render_search(console, call)
    color = "green" if task.would_post > 0 else "dim"
    label = f"{task.would_post} item" + ("s" if task.would_post != 1 else "")
    console.print(f"  [{color}]would post {label}[/{color}]  [dim](dry run)[/dim]")


def _render_feed(console, feed: FeedRecord) -> None:
    from rich.markup import escape
    from rich.panel import Panel

    url_excl = feed.url_excluded
    stats = (
        f"total={feed.total_in_feed}  new={feed.new_eligible}  "
        f"excl={len(url_excl)}  passed={feed.passed_heuristic}  "
        f"after_limit={feed.after_limit}"
    )
    lines = [f"[dim]{escape(feed.url)}[/dim]", stats]
    for e in url_excl:
        lines.append(f"  [yellow dim]\\[url-excl][/yellow dim] {escape(e['url'])}")
    for t in feed.title_transforms:
        lines.append(
            f"  [dim]\\[title-tr][/dim] {escape(repr(t['before'][:60]))} → {escape(repr(t['after'][:60]))}"
        )
    for t in feed.description_transforms:
        b = t["before"][:60].replace("\n", " ")
        a = t["after"][:60].replace("\n", " ")
        lines.append(f"  [dim]\\[desc-tr][/dim] {escape(repr(b))} → {escape(repr(a))}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold]Feed[/bold] [cyan]{escape(feed.name)}[/cyan]",
            border_style="blue",
            expand=False,
        )
    )


def _render_summarize(console, call: LLMCall) -> None:
    from rich.markup import escape
    from rich.panel import Panel

    summary = call.response or "(none)"
    content = f"[dim]{escape(call.item_url or '')}[/dim]\n\n{escape(summary)}"
    console.print(
        Panel(
            content,
            title=f"[bold]Summarize[/bold] [magenta]{escape((call.item_title or '')[:80])}[/magenta]",
            border_style="magenta",
            expand=False,
        )
    )


def _render_filter(console, call: LLMCall) -> None:
    from rich import box
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    payload = call.payload or []
    n_items = sum(len(g.get("items", [])) for g in payload)
    parsed = call.parsed or []

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim", expand=False)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("ID", width=4, no_wrap=True)
    table.add_column("Source", max_width=18, no_wrap=True)
    table.add_column("Title", ratio=2)
    table.add_column("Reason", ratio=3)

    for item in parsed:
        passed = item.get("pass", False)
        icon = Text("✓", style="green") if passed else Text("✗", style="red")
        title_text = Text(str(item.get("title", ""))[:70])
        if not passed:
            title_text.stylize("dim")
        table.add_row(
            icon,
            Text(str(item.get("id", "?"))),
            Text(str(item.get("source", "?"))),
            title_text,
            Text(str(item.get("reason", ""))[:200]),
        )

    footer_lines: list[str] = []
    if call.instructions:
        footer_lines.append(f"[dim]instructions:[/dim] {escape(str(call.instructions)[:200])}")
    if call.memory:
        footer_lines.append(f"[dim]memory:[/dim] {escape(str(call.memory)[:300])}")

    console.print(
        Panel(
            table if parsed else Text("(no items)", style="dim"),
            title=(
                f"[bold]LLM Filter[/bold]  "
                f"[dim]model={escape(str(call.model or ''))}  items={n_items}[/dim]"
            ),
            subtitle="\n".join(footer_lines) if footer_lines else None,
            border_style="yellow",
            expand=False,
        )
    )


def _render_search(console, call: LLMCall) -> None:
    from rich.markup import escape
    from rich.panel import Panel

    raw = call.response or "(no response)"
    lines = []
    if call.instructions:
        lines.append(f"[dim]instructions:[/dim] {escape(str(call.instructions)[:200])}")
    lines.append(f"[dim]prompt:[/dim] {escape((call.prompt or '')[:300])}")
    lines.append("")
    lines.append(escape(raw[:800]))
    console.print(
        Panel(
            "\n".join(lines),
            title=(f"[bold]LLM Search[/bold]  [dim]model={escape(str(call.model or ''))}[/dim]"),
            border_style="yellow",
            expand=False,
        )
    )
