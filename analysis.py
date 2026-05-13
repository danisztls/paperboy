import json
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class AnalysisCollector:
    limit: int = 7
    limit_feeds: int = 7
    _tasks: list[dict] = field(default_factory=list, repr=False)
    _current: dict | None = field(default=None, repr=False)

    def begin_task(self, name: str, task_type: str) -> None:
        self._current = {
            "task": name,
            "type": task_type,
            "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "feeds": [],
            "summarization": [],
            "llm_filter": None,
            "llm_search": None,
            "push": {"dry_run": True, "would_post": 0},
        }

    def record_feed(
        self,
        url: str,
        name: str,
        total_in_feed: int,
        new_eligible: int,
        after_limit: int,
        url_excluded: list[dict],
        title_transforms: list[dict],
        description_transforms: list[dict],
    ) -> None:
        if self._current is None:
            return
        self._current["feeds"].append(
            {
                "url": url,
                "name": name,
                "total_in_feed": total_in_feed,
                "new_eligible": new_eligible,
                "passed_heuristic": new_eligible - len(url_excluded),
                "after_limit": after_limit,
                "heuristic_filters": {
                    "url_excluded": url_excluded,
                    "title_transforms": title_transforms,
                    "description_transforms": description_transforms,
                },
            }
        )

    def record_filter(
        self,
        model: str | None,
        instructions: str,
        payload: list,
        raw_response: str | None,
        parsed: list[dict],
        memory: str | None,
    ) -> None:
        if self._current is None:
            return
        self._current["llm_filter"] = {
            "model": model,
            "instructions": instructions,
            "payload": payload,
            "raw_response": raw_response,
            "parsed": parsed,
            "memory": memory,
        }

    def record_summarization(
        self,
        item_id: str,
        title: str,
        url: str | None,
        fetched_body: str | None,
        instructions: str,
        input_text: str,
        summary: str | None,
    ) -> None:
        if self._current is None:
            return
        self._current["summarization"].append(
            {
                "id": item_id,
                "title": title,
                "url": url,
                "fetched_body": fetched_body,
                "instructions": instructions,
                "input": input_text,
                "summary": summary,
            }
        )

    def record_llm_search(
        self,
        model: str | None,
        instructions: str | None,
        prompt: str,
        raw_response: str | None,
    ) -> None:
        if self._current is None:
            return
        self._current["llm_search"] = {
            "model": model,
            "instructions": instructions,
            "prompt": prompt,
            "raw_response": raw_response,
        }

    def record_push(self, would_post: int) -> None:
        if self._current is None:
            return
        self._current["push"]["would_post"] = would_post

    def finish_task(self) -> None:
        if self._current is not None:
            self._tasks.append(self._current)
            self._current = None

    def display(self, *, console=None) -> None:
        from rich import box
        from rich.console import Console
        from rich.markup import escape
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        if console is None:
            console = Console()

        for task in self._tasks:
            console.print()
            console.rule(
                f"[bold cyan]{escape(task['task'])}[/bold cyan]  "
                f"[dim]{escape(task['type'])} · {escape(task['timestamp'])}[/dim]"
            )

            for feed in task.get("feeds", []):
                hf = feed.get("heuristic_filters", {})
                url_excl = hf.get("url_excluded", [])
                title_tr = hf.get("title_transforms", [])
                desc_tr = hf.get("description_transforms", [])

                stats = (
                    f"total={feed['total_in_feed']}  new={feed['new_eligible']}  "
                    f"excl={len(url_excl)}  passed={feed['passed_heuristic']}  "
                    f"after_limit={feed['after_limit']}"
                )
                lines = [f"[dim]{escape(feed['url'])}[/dim]", stats]
                for e in url_excl:
                    lines.append(f"  [yellow dim]\\[url-excl][/yellow dim] {escape(e['url'])}")
                for t in title_tr:
                    lines.append(
                        f"  [dim]\\[title-tr][/dim] {escape(repr(t['before'][:60]))} → {escape(repr(t['after'][:60]))}"
                    )
                for t in desc_tr:
                    b = t["before"][:60].replace("\n", " ")
                    a = t["after"][:60].replace("\n", " ")
                    lines.append(f"  [dim]\\[desc-tr][/dim] {escape(repr(b))} → {escape(repr(a))}")

                console.print(
                    Panel(
                        "\n".join(lines),
                        title=f"[bold]Feed[/bold] [cyan]{escape(feed['name'])}[/cyan]",
                        border_style="blue",
                        expand=False,
                    )
                )

            for s in task.get("summarization", []):
                summary = s.get("summary") or "(none)"
                content = f"[dim]{escape(s.get('url') or '')}[/dim]\n\n{escape(summary)}"
                console.print(
                    Panel(
                        content,
                        title=f"[bold]Summarize[/bold] [magenta]{escape(s['title'][:80])}[/magenta]",
                        border_style="magenta",
                        expand=False,
                    )
                )

            if task.get("llm_filter"):
                f = task["llm_filter"]
                payload = f.get("payload") or []
                n_items = sum(len(g.get("items", [])) for g in payload)
                parsed = f.get("parsed") or []

                table = Table(
                    box=box.SIMPLE, show_header=True, header_style="bold dim", expand=False
                )
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
                if f.get("instructions"):
                    footer_lines.append(
                        f"[dim]instructions:[/dim] {escape(str(f['instructions'])[:200])}"
                    )
                if f.get("memory"):
                    footer_lines.append(f"[dim]memory:[/dim] {escape(str(f['memory'])[:300])}")

                console.print(
                    Panel(
                        table if parsed else Text("(no items)", style="dim"),
                        title=(
                            f"[bold]LLM Filter[/bold]  "
                            f"[dim]model={escape(str(f.get('model') or ''))}  items={n_items}[/dim]"
                        ),
                        subtitle="\n".join(footer_lines) if footer_lines else None,
                        border_style="yellow",
                        expand=False,
                    )
                )

            if task.get("llm_search"):
                s = task["llm_search"]
                raw = s.get("raw_response") or "(no response)"
                lines = []
                if s.get("instructions"):
                    lines.append(f"[dim]instructions:[/dim] {escape(str(s['instructions'])[:200])}")
                lines.append(f"[dim]prompt:[/dim] {escape(s['prompt'][:300])}")
                lines.append("")
                lines.append(escape(raw[:800]))
                console.print(
                    Panel(
                        "\n".join(lines),
                        title=(
                            f"[bold]LLM Search[/bold]  "
                            f"[dim]model={escape(str(s.get('model') or ''))}[/dim]"
                        ),
                        border_style="yellow",
                        expand=False,
                    )
                )

            p = task["push"]
            would = p["would_post"]
            color = "green" if would > 0 else "dim"
            label = f"{would} item" + ("s" if would != 1 else "")
            console.print(f"  [{color}]would post {label}[/{color}]  [dim](dry run)[/dim]")

        console.print()

    def to_json(self) -> str:
        return json.dumps(self._tasks, ensure_ascii=False, indent=2, default=str)

    def to_human(self) -> str:
        lines: list[str] = []

        def _indent(text: str, prefix: str = "    ") -> str:
            return "\n".join(prefix + line for line in str(text).splitlines())

        for task in self._tasks:
            lines.append("=" * 72)
            lines.append(f"TASK: {task['task']}  type={task['type']}  {task['timestamp']}")
            lines.append("=" * 72)

            for feed in task.get("feeds", []):
                hf = feed.get("heuristic_filters", {})
                url_excl = hf.get("url_excluded", [])
                title_tr = hf.get("title_transforms", [])
                desc_tr = hf.get("description_transforms", [])
                lines.append(
                    f"\n=== FEED: {feed['name']}  ({feed['url']})\n"
                    f"    total_in_feed={feed['total_in_feed']}  new_eligible={feed['new_eligible']}  "
                    f"url_excluded={len(url_excl)}  passed_heuristic={feed['passed_heuristic']}  "
                    f"after_limit={feed['after_limit']}"
                )
                for e in url_excl:
                    lines.append(f"    [url-excluded]      {e['url']}")
                for t in title_tr:
                    lines.append(f"    [title-transform]   {t['before']!r}  →  {t['after']!r}")
                for t in desc_tr:
                    b = t["before"][:120].replace("\n", " ")
                    a = t["after"][:120].replace("\n", " ")
                    lines.append(f"    [desc-transform]    {b!r}  →  {a!r}")

            for s in task.get("summarization", []):
                lines.append(f"\n=== SUMMARIZE: {s['title'][:80]}")
                lines.append(f"    url: {s['url']}")
                fb = s.get("fetched_body") or ""
                if fb:
                    lines.append(f"    fetched_body ({len(fb)} chars):")
                    lines.append(_indent(fb[:400].replace("\n", " ")))
                lines.append("    instructions:")
                lines.append(_indent(s["instructions"][:300]))
                lines.append(f"    summary: {s.get('summary') or '(none)'}")

            if task.get("llm_filter"):
                f = task["llm_filter"]
                raw = f.get("raw_response") or ""
                payload = f.get("payload") or []
                n_items = sum(len(g.get("items", [])) for g in payload)
                lines.append(f"\n=== LLM FILTER  model={f['model']}  items={n_items}")
                lines.append("--- instructions:")
                lines.append(_indent(f["instructions"][:800]))
                lines.append("--- payload (JSON):")
                try:
                    payload_str = json.dumps(payload, ensure_ascii=False)
                except Exception:
                    payload_str = str(payload)
                lines.append(_indent(payload_str[:800]))
                lines.append(f"--- raw_response ({len(raw)} chars):")
                lines.append(_indent(raw[:800]))
                lines.append(f"--- parsed ({len(f['parsed'])} items):")
                for item in f["parsed"]:
                    icon = "✓" if item.get("pass") else "✗"
                    lines.append(
                        f"    [{icon}] [{item.get('id', '?')}] {item.get('source', '?')} — {str(item.get('title', ''))[:70]}"
                    )
                    reason = str(item.get("reason", ""))
                    if reason:
                        lines.append(f"         {reason[:140]}")
                if f.get("memory"):
                    lines.append("--- memory:")
                    lines.append(_indent(f["memory"][:500]))

            if task.get("llm_search"):
                s = task["llm_search"]
                raw = s.get("raw_response") or ""
                lines.append(f"\n=== LLM SEARCH  model={s['model']}")
                if s.get("instructions"):
                    lines.append("--- instructions:")
                    lines.append(_indent(str(s["instructions"])[:400]))
                lines.append("--- prompt:")
                lines.append(_indent(s["prompt"][:400]))
                lines.append(f"--- raw_response ({len(raw)} chars):")
                lines.append(_indent(raw[:800]))

            p = task["push"]
            lines.append(f"\n=== PUSH  dry_run={p['dry_run']}  would_post={p['would_post']}")

        return "\n".join(lines)
