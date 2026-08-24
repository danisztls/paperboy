# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Config primitives and raw-dict accessors (no validation — see schema.py)."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

_DURATION_UNITS = {"m": "minutes", "h": "hours"}
_CALENDAR_UNITS = ("d", "w")


@dataclass(frozen=True)
class Period:
    """A polling period. Unit decides comparison kind: m/h are sliding-window
    durations; d/w align to local calendar days/ISO weeks."""

    count: float
    unit: Literal["m", "h", "d", "w"]

    @property
    def is_calendar(self) -> bool:
        return self.unit in _CALENDAR_UNITS

    def as_timedelta(self) -> timedelta:
        if self.is_calendar:
            raise ValueError(f"{self} has no fixed duration; use calendar arithmetic")
        return timedelta(**{_DURATION_UNITS[self.unit]: self.count})

    def __str__(self) -> str:
        n = int(self.count) if float(self.count).is_integer() else self.count
        return f"{n}{self.unit}"


def parse_color(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.startswith("#") and len(s) == 7:
        return int(s[1:], 16)
    return None


def parse_period(value) -> Period:
    s = str(value).strip() if not isinstance(value, str) else value.strip()
    if not s:
        raise ValueError("empty period")
    suffix = s[-1].lower()
    if suffix not in _DURATION_UNITS and suffix not in _CALENDAR_UNITS:
        raise ValueError(f"missing suffix in {value!r} — use e.g. '30m', '6h', '1d', '1w'")
    raw = float(s[:-1])
    if suffix in _CALENDAR_UNITS:
        if not raw.is_integer() or raw <= 0:
            raise ValueError(
                f"calendar period {value!r} must be a positive integer count of '{suffix}'"
            )
        return Period(count=int(raw), unit=suffix)
    return Period(count=raw, unit=suffix)


def task_kind(task_cfg: dict) -> str:
    explicit = task_cfg.get("kind")
    if explicit:
        return explicit
    pull = task_cfg.get("pull", [])
    if any("realestate" in item for item in pull):
        return "realestate"
    if any("research" in item for item in pull):
        return "research"
    if any("weather" in item for item in pull):
        return "weather"
    if any("finance" in item for item in pull):
        return "finance"
    return "feeds"


_YT_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def is_youtube_feed_url(url: str) -> bool:
    """True for a YouTube channel feed URL (both the `youtube:` sugar and a verbose
    `feed:` pointing at one). Used to gate the `youtube:` scope blocks to YouTube feeds."""
    return url.startswith("https://www.youtube.com/feeds/videos.xml")


def _youtube_to_feed(yt: dict) -> dict:
    """Expand a `youtube` pull item into a `feed` dict (sugar over feed).

    The URL is built from `channel_id` and is byte-identical to the verbose
    `feeds/videos.xml?channel_id=...` form, so feed state (keyed by url) is preserved.
    Every other key (`ignore`/`skip`/`description`/`title`/…) carries through unchanged;
    `shorts`/`livestreams` self-gate to YouTube and the global `youtube:` scope merges in.
    """
    feed = {k: v for k, v in yt.items() if k != "channel_id"}
    feed["url"] = _YT_FEED_URL.format(yt["channel_id"])
    return feed


def get_feeds(task_cfg: dict) -> list[dict]:
    feeds: list[dict] = []
    for item in task_cfg.get("pull", []):
        if "feed" in item:
            feeds.append(item["feed"])
        elif "youtube" in item:
            feeds.append(_youtube_to_feed(item["youtube"]))
    return feeds


def get_realestate_cfgs(task_cfg: dict) -> list[dict]:
    return [item["realestate"] for item in task_cfg.get("pull", []) if "realestate" in item]


def get_discord_cfg(task_cfg: dict) -> dict:
    return next((item["discord"] for item in task_cfg.get("push", []) if "discord" in item), {})


def get_research_cfg(task_cfg: dict) -> dict:
    return next((item["research"] for item in task_cfg.get("pull", []) if "research" in item), {})


def get_weather_cfg(task_cfg: dict) -> dict:
    return next((item["weather"] for item in task_cfg.get("pull", []) if "weather" in item), {})


def get_finance_cfg(task_cfg: dict) -> dict:
    return next((item["finance"] for item in task_cfg.get("pull", []) if "finance" in item), {})


def get_file_path(task_cfg: dict) -> str | None:
    return next((item["file"] for item in task_cfg.get("push", []) if "file" in item), None)


def get_api_key_for_provider(api_key_cfg, provider: str | None) -> str | None:
    """Return the API key for a given provider from a {deepseek, gemini} dict."""
    if api_key_cfg is None or not provider:
        return None
    return api_key_cfg.get(provider)
