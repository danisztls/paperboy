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
