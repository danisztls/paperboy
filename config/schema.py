"""Pydantic validation of the full config file.

Any change that adds, removes, or renames a config key must be reflected here
so `--validate` stays in sync with what the code reads.
"""

import json
import logging
import pathlib
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.functional_validators import AfterValidator, BeforeValidator

from config.values import parse_color, parse_period

log = logging.getLogger(__name__)


# --- Annotated constraint types ---


def _color_validator(v):
    result = parse_color(v)
    if v is not None and result is None:
        raise ValueError(f"invalid color {v!r} — expected '#RRGGBB'")
    return result


def _period_validator(v):
    if v is not None:
        try:
            parse_period(v)
        except ValueError, TypeError:
            raise ValueError(f"invalid value {v!r} — expected e.g. '30m', '6h', '1d', '1w'")
    return v


_Color = Annotated[int | None, BeforeValidator(_color_validator)]
_Period = Annotated[str | None, AfterValidator(_period_validator)]


# --- Model capability registry ---

Provider = Literal["deepseek", "gemini", "claude_cli"]
ReasoningLevel = Literal["off", "low", "medium", "high"]


def _load_models_registry() -> dict[str, dict[str, dict]]:
    path = pathlib.Path(__file__).resolve().parent.parent / "providers" / "llm" / "models.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("models.json not found at %s — model capability checks disabled", path)
        return {}


_MODELS_REGISTRY = _load_models_registry()


class ModelSpec(BaseModel):
    """Verbose model spec: {provider, name, reasoning?}.

    `reasoning` is plumbed through every LLM call; adapters translate the level
    to provider-specific dicts (effort / budget_tokens / thinking_budget).
    """

    model_config = ConfigDict(extra="forbid")
    provider: Provider
    name: str
    reasoning: ReasoningLevel | None = None

    @model_validator(mode="after")
    def _check_against_registry(self):
        provider_models = _MODELS_REGISTRY.get(self.provider, {})
        if not provider_models:
            return self
        entry = provider_models.get(self.name)
        if entry is None:
            log.warning(
                "Model %r is not in providers/llm/models.json under %r — "
                "capability checks skipped (add it if it's a known model)",
                self.name,
                self.provider,
            )
            return self
        if self.reasoning in ("low", "medium", "high") and not entry.get("thinking"):
            raise ValueError(
                f"model {self.provider}:{self.name} does not support thinking — "
                f"remove reasoning: {self.reasoning} or pick a thinking-capable model"
            )
        if entry.get("deprecated"):
            log.warning(
                "Model %s:%s is marked deprecated in providers/llm/models.json — "
                "still functional but should be replaced",
                self.provider,
                self.name,
            )
        return self


def resolve_model_specs(spec) -> list[ModelSpec]:
    """Return list of ModelSpec from a raw config value (single dict or list of dicts)."""
    if spec is None:
        return []
    raw = spec if isinstance(spec, list) else [spec]
    return [ModelSpec.model_validate(s) for s in raw if s is not None]


def _normalize_model_spec_list(v):
    if v is None or isinstance(v, list):
        return v
    return [v]


ModelSpecList = Annotated[list[ModelSpec] | None, BeforeValidator(_normalize_model_spec_list)]


# --- Global sections ---


class _GlobalDiscord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: _Color = None


class _Feeds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_days: int = 7


class _Retention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = 30  # 0 disables pruning


class _ApiKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deepseek: str | None = None
    gemini: str | None = None
    # Optional: setting this opts into direct Anthropic API billing for the claude_cli
    # provider. Omit to reuse the existing Claude Code login (subscription OAuth).
    claude_cli: str | None = None


class _GlobalLLM(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: _ApiKeys | None = None


class _GlobalCurate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSpecList = None
    language: str | None = None


class _GlobalResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSpecList = None
    instructions: str | None = None


class _GlobalSummarize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSpecList = None


# --- Scoped heuristic blocks (global / task / feed) ---


class _TextTransform(BaseModel):
    """Heuristic regex transforms applied to a single text field (`description`/`title`).

    A flat op-set (no chaining): at least one of `remove` / `extract` / `replace`.
    `remove` is a raw regex (or list) `re.sub`'d out; `extract` keeps the match;
    `replace`/`with` is a plain `re.sub`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    remove: Any = None
    extract: str | None = None
    replace: str | None = None
    with_: str | None = Field(None, alias="with")

    @model_validator(mode="after")
    def _has_op(self):
        if not ({"remove", "extract", "replace"} & self.model_fields_set):
            raise ValueError("no recognized operation key (remove/extract/replace)")
        return self

    @field_validator("remove", "extract", "replace", mode="before")
    @classmethod
    def _valid_regex(cls, v):
        for pat in v if isinstance(v, list) else [v]:
            if isinstance(pat, str):
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise ValueError(f"invalid regex {pat!r}: {exc}")
        return v


class _Ignore(BaseModel):
    """Omit a whole FIELD from the post."""

    model_config = ConfigDict(extra="forbid")
    image: bool | None = None
    description: bool | None = None


class _Skip(BaseModel):
    """Omit a whole ENTRY. `shorts`/`livestreams` self-gate to YouTube by URL."""

    model_config = ConfigDict(extra="forbid")
    shorts: bool | None = None
    livestreams: bool | None = None
    url_contains: str | list[str] | None = None


class _YouTube(BaseModel):
    """YouTube-feeds-only scope: reuses the `ignore`/`skip` vocabulary, applied only
    to feeds whose URL is a YouTube channel feed (see `is_youtube_feed_url`)."""

    model_config = ConfigDict(extra="forbid")
    ignore: _Ignore | None = None
    skip: _Skip | None = None


# --- Pull items ---


class _FeedDiscord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: _Color = None


class _Summarize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = None
    instructions: str | None = None


class _FeedCurate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skip: bool | None = None


class _PullFeedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    url: str
    discord: _FeedDiscord = Field(default_factory=_FeedDiscord)
    ignore: _Ignore | None = None
    skip: _Skip | None = None
    description: _TextTransform | None = None
    title: _TextTransform | None = None
    youtube: _YouTube | None = None
    summarize: bool | _Summarize | None = None
    curate: _FeedCurate | None = None


class _PullYouTubeItem(BaseModel):
    """Sugar over `feed`: a YouTube channel by `channel_id` (the feed URL is built in
    `get_feeds`). Full feed parity (ignore/skip/description/title/summarize/curate);
    the entry is itself a YouTube feed, so its `ignore`/`skip` apply directly and the
    global `youtube:` scope merges in — no nested `youtube:` block needed."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    channel_id: str
    discord: _FeedDiscord = Field(default_factory=_FeedDiscord)
    ignore: _Ignore | None = None
    skip: _Skip | None = None
    description: _TextTransform | None = None
    title: _TextTransform | None = None
    summarize: bool | _Summarize | None = None
    curate: _FeedCurate | None = None


class _PullResearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    model: ModelSpec | None = None
    instructions: str | None = None
    # Agent-loop limits (termination guarantees, not cost tracking).
    max_steps: int = 6
    max_searches: int = 3
    max_reads: int = 6
    max_results: int = 8  # search results pulled per query
    read_top: int = 5  # passages extracted per read


class _PullRealestateItem(BaseModel):
    model_config = ConfigDict(extra="allow")  # provider-specific keys are allowed
    url: str
    max_items: int | None = None
    exclude_neighborhoods: list[str] | None = None


class _PullWeatherItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float
    longitude: float
    location_name: str
    timezone: str
    uv_warn_threshold: int = 6
    forecast_days: int = 7
    kind: Literal["smart"] | None = None


class _PullFinanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stocks: list[str]


class _PullFinanceMonitorRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    delta: float
    price: tuple[float, float] | None = None
    exchange: Literal["us_equity", "b3", "fx", "crypto"] | None = None


class _PullFinanceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: _PullFinanceReport | None = None
    monitor: list[_PullFinanceMonitorRule] | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        present = [k for k in ("report", "monitor") if k in self.model_fields_set]
        if len(present) != 1:
            raise ValueError("finance pull item must have exactly one key: 'report' or 'monitor'")
        return self


class _PullItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed: _PullFeedItem | None = None
    youtube: _PullYouTubeItem | None = None
    research: _PullResearchItem | None = None
    realestate: _PullRealestateItem | None = None
    weather: _PullWeatherItem | None = None
    finance: _PullFinanceItem | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        present = [
            k
            for k in ("feed", "youtube", "research", "realestate", "weather", "finance")
            if k in self.model_fields_set
        ]
        if len(present) != 1:
            raise ValueError(
                "each pull item must have exactly one key: 'feed', 'youtube', 'research', 'realestate', 'weather', or 'finance'"
            )
        return self


# --- Push items ---


class _PushDiscordItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhook: str
    color: _Color = None
    format: Literal["embed", "markdown"] | None = None
    wrap: bool = True


class _PushItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord: _PushDiscordItem | None = None
    file: str | None = None

    @field_validator("file")
    @classmethod
    def _validate_file_ext(cls, v):
        if v is None:
            return v
        ext = pathlib.Path(v).suffix.lower()
        if ext not in {".md", ".jsonl"}:
            raise ValueError(f"file push target must end in .md or .jsonl (got {v!r})")
        return v

    @model_validator(mode="after")
    def _has_target(self):
        if not self.model_fields_set:
            raise ValueError("each push item must have a target key (e.g. 'discord', 'file')")
        return self


# --- Tasks ---


class _CurateCorroborate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    max_steps: int | None = None
    max_searches: int | None = None
    max_results: int | None = None


class _TaskCurate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criteria: str
    model: ModelSpec | None = None
    language: str | None = None
    instructions: str | None = None
    explain: bool | None = None
    corroborate: _CurateCorroborate | None = None


class _Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["digest", "realestate", "weather", "finance"] | None = None
    period: _Period = None
    pull: list[_PullItem]
    push: list[_PushItem]
    ignore: _Ignore | None = None
    skip: _Skip | None = None
    description: _TextTransform | None = None
    title: _TextTransform | None = None
    youtube: _YouTube | None = None
    curate: _TaskCurate | None = None
    summarize: bool | _Summarize | None = None

    @model_validator(mode="after")
    def _check_task(self):
        has_research_pull = any(item.research is not None for item in self.pull)
        # youtube is sugar over feed (get_feeds expands it), so it's feed-family for mixing.
        has_feed_pull = any(item.feed is not None or item.youtube is not None for item in self.pull)
        has_realestate_pull = any(item.realestate is not None for item in self.pull)
        has_weather_pull = any(item.weather is not None for item in self.pull)
        has_finance_pull = any(item.finance is not None for item in self.pull)
        has_discord_push = any(item.discord is not None for item in self.push)
        has_file_push = any(item.file is not None for item in self.push)
        if not (has_discord_push or has_file_push):
            raise ValueError("push must contain at least one target (discord or file)")
        if has_realestate_pull and (has_research_pull or has_feed_pull):
            raise ValueError("pull cannot mix realestate with feed or research items")
        if has_realestate_pull:
            seen_urls: set[str] = set()
            for item in self.pull:
                if item.realestate is None:
                    continue
                url = item.realestate.url
                if url in seen_urls:
                    raise ValueError(f"pull has duplicate realestate url: {url}")
                seen_urls.add(url)
        if has_research_pull and has_feed_pull:
            raise ValueError("pull cannot mix feed and research items")
        if has_weather_pull and (has_feed_pull or has_research_pull or has_realestate_pull):
            raise ValueError("pull cannot mix weather with other pull types")
        if has_weather_pull and sum(1 for item in self.pull if item.weather is not None) > 1:
            raise ValueError("pull can have at most one weather item")
        if has_finance_pull and (
            has_feed_pull or has_research_pull or has_realestate_pull or has_weather_pull
        ):
            raise ValueError("pull cannot mix finance with other pull types")
        if has_finance_pull and sum(1 for item in self.pull if item.finance is not None) > 1:
            raise ValueError("pull can have at most one finance item")
        return self


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord: _GlobalDiscord | None = None
    feeds: _Feeds | None = None
    llm: _GlobalLLM | None = None
    curate: _GlobalCurate | None = None
    research: _GlobalResearch | None = None
    summarize: _GlobalSummarize | None = None
    ignore: _Ignore | None = None
    skip: _Skip | None = None
    description: _TextTransform | None = None
    title: _TextTransform | None = None
    youtube: _YouTube | None = None
    retention: _Retention | None = None
    tasks: list[_Task]


def _fmt_loc(loc: tuple) -> str:
    parts = []
    for p in loc:
        parts.append(f"[{p}]" if isinstance(p, int) else ("." + p if parts else p))
    return "".join(parts)


def validate_config(config: dict) -> list[str]:
    try:
        _Config.model_validate(config)
        return []
    except ValidationError as exc:
        return [
            f"{_fmt_loc(e['loc'])}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors()
        ]
