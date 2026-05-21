"""Abstract pipeline: Source → (Processor) → Target."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple


class Citation(NamedTuple):
    """A source name + optional URL pair."""

    source: str
    url: str | None


class MemoryParagraph(NamedTuple):
    """One paragraph of the memory briefing with its supporting citation IDs."""

    text: str
    citations: list[int]
    section: str | None = None  # thematic heading; set only on the first para of a group


@dataclass
class Item:
    """Generic content item produced by a Source and consumed by a Target."""

    id: str
    title: str
    source: str  # display name of the originating source
    url: str | None = None
    body: str | None = None  # sanitized text content
    image: str | None = None
    images: list[str] = field(default_factory=list)
    published: datetime | None = None
    summary: str | None = None  # optional LLM-generated summary
    filter_pass: bool | None = None
    filter_reason: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class PullResult:
    """Output of Source.pull()."""

    new_items: list[Item]  # unseen items in chronological order
    current_items: list[dict]  # all items currently in source (url+title, for state)
    name: str | None = None  # display name of the source (set by feed sources, for state)


@dataclass
class FilterResult:
    """Output of the LLM filter step."""

    items: list[Item]  # all input items with filter_pass/filter_reason set
    memory: list[MemoryParagraph] | None  # new memory briefing (for digest targets)
    cite_map: dict[int, Citation]  # LLM int ID → Citation(source, url)


@dataclass
class PushContext:
    """Input to Target.push()."""

    items: list[Item]
    memory: list[MemoryParagraph] | None = None
    cite_map: dict[int, Citation] | None = None


class Source(ABC):
    """Pulls items from an external source."""

    @abstractmethod
    async def pull(
        self,
        cfg: dict,
        seen: set[str],
        session,
    ) -> PullResult | None:
        """Fetch new items. Returns None on failure; caller must not update state."""
        ...


class Target(ABC):
    """Publishes items to an external destination."""

    @abstractmethod
    async def push(
        self,
        ctx: PushContext,
        cfg: dict,
        session,
    ) -> set[str]:
        """Publish items. Returns IDs of items that failed to publish."""
        ...
