"""Per-run dependency bundle threaded through every task processor.

`RunContext` replaces the long keyword plumbing (session, config, collector,
analysis flag, LLM adapters/models/reasoning) that every `process_*_task`
used to take individually. Built once in `main._async_main`; tests construct
it with just a session and rely on the defaults.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp

from providers.llm.base import ModelHandle

if TYPE_CHECKING:
    from evals.capture import RunCapture


@dataclass
class LLMHandles:
    """The three globally-configured LLM sections, resolved to callable handles."""

    curate: ModelHandle | None = None
    summarize: ModelHandle | None = None
    research: ModelHandle | None = None


@dataclass
class RunContext:
    session: aiohttp.ClientSession
    config: dict = field(default_factory=dict)  # full global config
    llm: LLMHandles = field(default_factory=LLMHandles)
    collector: RunCapture | None = None
    analysis: bool = False  # inspection mode: dry-run, reasoning on, truncated input
    force: bool = False  # --task <name>: run regardless of period (task and per-feed)

    @property
    def language(self) -> str:
        return (self.config.get("curate") or {}).get("language") or "EN-US"

    @property
    def max_age_seconds(self) -> int:
        return int((self.config.get("feeds") or {}).get("max_age_days") or 7) * 86400

    @property
    def research_instructions(self) -> str | None:
        return (self.config.get("research") or {}).get("instructions") or None

    def record_push(self, count: int) -> None:
        if self.collector:
            self.collector.record_push(count)

    @contextmanager
    def capture_task(self, name: str, kind: str):
        """Bracket a task run for the eval-trace collector."""
        if self.collector:
            self.collector.begin_task(name, kind)
        try:
            yield
        finally:
            if self.collector:
                self.collector.finish_task()
