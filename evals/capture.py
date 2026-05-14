import json
import pathlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime


@dataclass
class FeedRecord:
    url: str
    name: str
    total_in_feed: int
    new_eligible: int
    after_limit: int
    url_excluded: list[dict]
    title_transforms: list[dict]
    description_transforms: list[dict]

    @property
    def passed_heuristic(self) -> int:
        return self.new_eligible - len(self.url_excluded)


@dataclass(kw_only=True)
class LLMCall:
    """One LLM round-trip captured during a task run.

    Fields irrelevant to a given call_type are left None and dropped at
    serialization time, so JSONL records keep the call-type-specific shape
    that replay.py expects.
    """

    task: str
    call_type: str  # "filter" | "summarize" | "llm_search"
    ts: str
    instructions: str | None = None
    response: str | None = None
    model_used: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float | None = None
    reasoning: str | None = None
    web_search: bool = False

    # filter-specific
    model: str | None = None  # configured spec
    payload: list | None = None
    parsed: list | None = None
    memory: str | None = None

    # summarize-specific
    input: str | None = None
    item_id: str | None = None
    item_title: str | None = None
    item_url: str | None = None
    fetched_body: str | None = None

    # llm_search-specific
    prompt: str | None = None

    def to_record(self) -> dict:
        """Project to the JSONL shape: only fields relevant to call_type."""
        common = {
            "task": self.task,
            "call_type": self.call_type,
            "ts": self.ts,
            "model_used": self.model_used,
            "instructions": self.instructions,
            "response": self.response,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_s": self.latency_s,
            "reasoning": self.reasoning,
            "web_search": self.web_search,
        }
        if self.call_type == "filter":
            payload = self.payload or []
            parsed = self.parsed or []
            return {
                **common,
                "model": self.model,
                "payload": payload,
                "parsed": parsed,
                "memory": self.memory,
                "source_groups_count": len(payload),
                "items_count": sum(len(g.get("items", [])) for g in payload),
                "passing_count": sum(1 for p in parsed if p.get("pass")),
            }
        if self.call_type == "summarize":
            return {
                **common,
                "input": self.input,
                "item_id": self.item_id,
                "item_title": self.item_title,
                "item_url": self.item_url,
                "fetched_body": self.fetched_body,
            }
        if self.call_type == "llm_search":
            return {**common, "model": self.model, "prompt": self.prompt}
        return common


@dataclass
class TaskCapture:
    task: str
    type: str
    timestamp: str
    feeds: list[FeedRecord] = field(default_factory=list)
    calls: list[LLMCall] = field(default_factory=list)
    would_post: int = 0


@dataclass
class RunCapture:
    limit: int = 7
    limit_feeds: int = 7
    _tasks: list[TaskCapture] = field(default_factory=list, repr=False)
    _current: TaskCapture | None = field(default=None, repr=False)

    def begin_task(self, name: str, kind: str) -> None:
        self._current = TaskCapture(
            task=name,
            type=kind,
            timestamp=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )

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
        self._current.feeds.append(
            FeedRecord(
                url=url,
                name=name,
                total_in_feed=total_in_feed,
                new_eligible=new_eligible,
                after_limit=after_limit,
                url_excluded=url_excluded,
                title_transforms=title_transforms,
                description_transforms=description_transforms,
            )
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
        self._current.calls.append(
            LLMCall(
                task=self._current.task,
                call_type="filter",
                ts=self._current.timestamp,
                instructions=instructions,
                response=raw_response,
                model_used=model_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=latency_s,
                reasoning=reasoning,
                web_search=web_search,
                model=model,
                payload=payload,
                parsed=parsed,
                memory=memory,
            )
        )

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
        self._current.calls.append(
            LLMCall(
                task=self._current.task,
                call_type="summarize",
                ts=self._current.timestamp,
                instructions=instructions,
                response=summary,
                model_used=model_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=latency_s,
                reasoning=reasoning,
                input=input_text,
                item_id=item_id,
                item_title=title,
                item_url=url,
                fetched_body=fetched_body,
            )
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
        self._current.calls.append(
            LLMCall(
                task=self._current.task,
                call_type="llm_search",
                ts=self._current.timestamp,
                instructions=instructions,
                response=raw_response,
                model_used=model_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=latency_s,
                reasoning=reasoning,
                web_search=web_search,
                model=model,
                prompt=prompt,
            )
        )

    def record_push(self, would_post: int) -> None:
        if self._current is None:
            return
        self._current.would_post = would_post

    def finish_task(self) -> None:
        if self._current is not None:
            self._tasks.append(self._current)
            self._current = None

    def write_jsonl(self, base_dir: pathlib.Path, run_iso: str) -> list[pathlib.Path]:
        """Persist captured LLM calls as <base_dir>/<task>/<run_iso>.jsonl.

        Tasks that produced zero LLM calls are skipped (no empty files).
        """
        written: list[pathlib.Path] = []
        for task in self._tasks:
            if not task.calls:
                continue
            out_dir = base_dir / task.task
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{run_iso}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for call in task.calls:
                    f.write(json.dumps(call.to_record(), ensure_ascii=False, default=str) + "\n")
            written.append(path)
        return written

    def to_jsonl_records(self) -> list[tuple[str, dict]]:
        """Flatten captured tasks to (task_name, record) tuples, one per LLM call."""
        return [(task.task, call.to_record()) for task in self._tasks for call in task.calls]

    def display(self, *, console=None) -> None:
        from evals.render import render_run

        render_run(self._tasks, console=console)

    def to_json(self) -> str:
        return json.dumps(
            [asdict(task) for task in self._tasks], ensure_ascii=False, indent=2, default=str
        )
