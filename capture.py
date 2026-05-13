import json
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RunCapture:
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
        *,
        model_used: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_s: float | None = None,
        reasoning: str | None = None,
        web_search: bool = False,
    ) -> None:
        if self._current is None:
            return
        self._current["llm_filter"] = {
            "model": model,
            "model_used": model_used,
            "instructions": instructions,
            "payload": payload,
            "raw_response": raw_response,
            "parsed": parsed,
            "memory": memory,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_s": latency_s,
            "reasoning": reasoning,
            "web_search": web_search,
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
        *,
        model_used: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_s: float | None = None,
        reasoning: str | None = None,
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
                "model_used": model_used,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_s": latency_s,
                "reasoning": reasoning,
            }
        )

    def record_llm_search(
        self,
        model: str | None,
        instructions: str | None,
        prompt: str,
        raw_response: str | None,
        *,
        model_used: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_s: float | None = None,
        reasoning: str | None = None,
        web_search: bool = True,
    ) -> None:
        if self._current is None:
            return
        self._current["llm_search"] = {
            "model": model,
            "model_used": model_used,
            "instructions": instructions,
            "prompt": prompt,
            "raw_response": raw_response,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_s": latency_s,
            "reasoning": reasoning,
            "web_search": web_search,
        }

    def record_push(self, would_post: int) -> None:
        if self._current is None:
            return
        self._current["push"]["would_post"] = would_post

    def finish_task(self) -> None:
        if self._current is not None:
            self._tasks.append(self._current)
            self._current = None

    def write_jsonl(self, base_dir: pathlib.Path, run_iso: str) -> list[pathlib.Path]:
        """Persist captured LLM calls as <base_dir>/<task>/<run_iso>.jsonl. Returns paths written.

        Tasks that produced zero LLM calls are skipped (no empty files).
        """
        by_task: dict[str, list[dict]] = {}
        for task_name, record in self.to_jsonl_records():
            by_task.setdefault(task_name, []).append(record)
        written: list[pathlib.Path] = []
        for task_name, recs in by_task.items():
            if not recs:
                continue
            out_dir = base_dir / task_name
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{run_iso}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            written.append(path)
        return written

    def to_jsonl_records(self) -> list[tuple[str, dict]]:
        """Flatten captured tasks to (task_name, record) tuples, one per LLM call.

        The caller decides how to group/persist these (e.g. per-task JSONL files).
        """
        records: list[tuple[str, dict]] = []
        for task in self._tasks:
            task_name = task["task"]
            ts = task["timestamp"]
            for s in task.get("summarization", []):
                records.append(
                    (
                        task_name,
                        {
                            "task": task_name,
                            "call_type": "summarize",
                            "ts": ts,
                            "model_used": s.get("model_used"),
                            "instructions": s.get("instructions"),
                            "input": s.get("input"),
                            "response": s.get("summary"),
                            "input_tokens": s.get("input_tokens"),
                            "output_tokens": s.get("output_tokens"),
                            "latency_s": s.get("latency_s"),
                            "reasoning": s.get("reasoning"),
                            "item_id": s.get("id"),
                            "item_title": s.get("title"),
                            "item_url": s.get("url"),
                            "fetched_body": s.get("fetched_body"),
                        },
                    )
                )
            f = task.get("llm_filter")
            if f:
                payload = f.get("payload") or []
                source_groups = len(payload)
                source_groups_items = sum(len(g.get("items", [])) for g in payload)
                parsed = f.get("parsed") or []
                passing = sum(1 for p in parsed if p.get("pass"))
                records.append(
                    (
                        task_name,
                        {
                            "task": task_name,
                            "call_type": "filter",
                            "ts": ts,
                            "model": f.get("model"),
                            "model_used": f.get("model_used"),
                            "instructions": f.get("instructions"),
                            "payload": payload,
                            "response": f.get("raw_response"),
                            "parsed": parsed,
                            "memory": f.get("memory"),
                            "input_tokens": f.get("input_tokens"),
                            "output_tokens": f.get("output_tokens"),
                            "latency_s": f.get("latency_s"),
                            "reasoning": f.get("reasoning"),
                            "source_groups_count": source_groups,
                            "items_count": source_groups_items,
                            "passing_count": passing,
                            "web_search": f.get("web_search", False),
                        },
                    )
                )
            ls = task.get("llm_search")
            if ls:
                records.append(
                    (
                        task_name,
                        {
                            "task": task_name,
                            "call_type": "llm_search",
                            "ts": ts,
                            "model": ls.get("model"),
                            "model_used": ls.get("model_used"),
                            "instructions": ls.get("instructions"),
                            "prompt": ls.get("prompt"),
                            "response": ls.get("raw_response"),
                            "input_tokens": ls.get("input_tokens"),
                            "output_tokens": ls.get("output_tokens"),
                            "latency_s": ls.get("latency_s"),
                            "reasoning": ls.get("reasoning"),
                            "web_search": ls.get("web_search", True),
                        },
                    )
                )
        return records

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
