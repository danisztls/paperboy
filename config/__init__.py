import json
import logging
import pathlib
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.functional_validators import AfterValidator, BeforeValidator

log = logging.getLogger(__name__)

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
    if any("scraper" in item for item in pull):
        return "scraper"
    if any("search" in item for item in pull):
        return "search"
    if any("weather" in item for item in pull):
        return "weather"
    if any("finance" in item for item in pull):
        return "finance"
    return "feeds"


def get_feeds(task_cfg: dict) -> list[dict]:
    return [item["feed"] for item in task_cfg.get("pull", []) if "feed" in item]


def get_discord_cfg(task_cfg: dict) -> dict:
    return next((item["discord"] for item in task_cfg.get("push", []) if "discord" in item), {})


def get_search_cfg(task_cfg: dict) -> dict:
    return next((item["search"] for item in task_cfg.get("pull", []) if "search" in item), {})


def get_weather_cfg(task_cfg: dict) -> dict:
    return next((item["weather"] for item in task_cfg.get("pull", []) if "weather" in item), {})


def get_finance_cfg(task_cfg: dict) -> dict:
    return next((item["finance"] for item in task_cfg.get("pull", []) if "finance" in item), {})


def get_file_path(task_cfg: dict) -> str | None:
    return next((item["file"] for item in task_cfg.get("push", []) if "file" in item), None)


def resolve_model_specs(spec) -> list[ModelSpec]:
    """Return list of ModelSpec from a raw config value (single dict or list of dicts)."""
    if spec is None:
        return []
    raw = spec if isinstance(spec, list) else [spec]
    return [ModelSpec.model_validate(s) for s in raw if s is not None]


def get_api_key_for_provider(api_key_cfg, provider: str | None) -> str | None:
    """Return the API key for a given provider from an {openai, gemini} dict."""
    if api_key_cfg is None or not provider:
        return None
    return api_key_cfg.get(provider)


def _make_secret_loader(secrets: dict | None, secrets_path: pathlib.Path) -> type:
    import yaml

    class _SecretLoader(yaml.SafeLoader):
        pass

    def _secret_constructor(loader, node):
        key = loader.construct_scalar(node)
        if secrets is None:
            raise ValueError(f"!secret {key!r} used in config but {secrets_path} does not exist")
        if key not in secrets:
            raise ValueError(f"!secret {key!r} not found in {secrets_path}")
        return secrets[key]

    _SecretLoader.add_constructor("!secret", _secret_constructor)
    return _SecretLoader


def load_config(path: pathlib.Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        secrets_path = path.parent / "secrets.yaml"
        secrets = yaml.safe_load(secrets_path.read_text()) or {} if secrets_path.exists() else None
        return yaml.load(text, Loader=_make_secret_loader(secrets, secrets_path))
    return json.loads(text)


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


# --- Models ---


class _GlobalDiscord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: _Color = None


class _Feeds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_days: int = 7


class _Retention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = 30  # 0 disables pruning


Provider = Literal["openai", "gemini", "deepseek", "anthropic"]
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


def _normalize_model_spec_list(v):
    if v is None or isinstance(v, list):
        return v
    return [v]


ModelSpecList = Annotated[list[ModelSpec] | None, BeforeValidator(_normalize_model_spec_list)]


class _ApiKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openai: str | None = None
    gemini: str | None = None
    deepseek: str | None = None
    anthropic: str | None = None


class _GlobalLLM(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: _ApiKeys | None = None


class _GlobalCurate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSpecList = None
    language: str | None = None


class _GlobalSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSpecList = None
    instructions: str | None = None


class _GlobalSummarize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelSpecList = None


class _Youtube(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookies_from_browser: str | None = None
    cookies_browser_profile: str | None = None


class _FilterOp(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    remove_phrases_with_urls: Any = None
    remove_phrases_containing: Any = None
    extract: str | None = None
    replace: str | None = None
    with_: str | None = Field(None, alias="with")
    clear: bool | None = None

    @model_validator(mode="after")
    def _has_op(self):
        ops = {
            "remove_phrases_with_urls",
            "remove_phrases_containing",
            "extract",
            "replace",
            "clear",
        }
        if not (ops & self.model_fields_set):
            raise ValueError("no recognized operation key")
        return self

    @field_validator("extract", "replace", mode="before")
    @classmethod
    def _valid_regex(cls, v):
        if isinstance(v, str):
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regex {v!r}: {exc}")
        return v


class _UrlFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skip_containing: str | list[str] | None = None

    @model_validator(mode="after")
    def _has_op(self):
        if not self.model_fields_set:
            raise ValueError("no recognized operation key")
        return self


class _FilterDict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: list[_FilterOp] | _FilterOp | None = None
    description: list[_FilterOp] | _FilterOp | None = None
    url: _UrlFilter | None = None


class _FeedDiscord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: _Color = None


class _Image(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skip: bool | None = None


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
    image: _Image | None = None
    filter: _FilterDict | None = None
    summarize: bool | _Summarize | None = None
    curate: _FeedCurate | None = None


class _PullSearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    model: ModelSpec | None = None
    web_search: bool | dict = True
    instructions: str | None = None


class _PullScraperItem(BaseModel):
    model_config = ConfigDict(extra="allow")  # adapter-specific keys are allowed
    adapter: str
    url: str
    max_items: int | None = None


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
    search: _PullSearchItem | None = None
    scraper: _PullScraperItem | None = None
    weather: _PullWeatherItem | None = None
    finance: _PullFinanceItem | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        present = [
            k
            for k in ("feed", "search", "scraper", "weather", "finance")
            if k in self.model_fields_set
        ]
        if len(present) != 1:
            raise ValueError(
                "each pull item must have exactly one key: 'feed', 'search', 'scraper', 'weather', or 'finance'"
            )
        return self


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


class _TaskCurate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criteria: str
    model: ModelSpec | None = None
    language: str | None = None
    instructions: str | None = None
    web_search: bool | dict | None = None
    explain: bool | None = None


class _Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["digest", "scraper", "weather", "finance"] | None = None
    period: _Period = None
    pull: list[_PullItem]
    push: list[_PushItem]
    image: _Image | None = None
    filter: _FilterDict | None = None
    curate: _TaskCurate | None = None
    summarize: bool | _Summarize | None = None

    @model_validator(mode="after")
    def _check_task(self):
        has_search_pull = any(item.search is not None for item in self.pull)
        has_feed_pull = any(item.feed is not None for item in self.pull)
        has_scraper_pull = any(item.scraper is not None for item in self.pull)
        has_weather_pull = any(item.weather is not None for item in self.pull)
        has_finance_pull = any(item.finance is not None for item in self.pull)
        has_discord_push = any(item.discord is not None for item in self.push)
        has_file_push = any(item.file is not None for item in self.push)
        if not (has_discord_push or has_file_push):
            raise ValueError("push must contain at least one target (discord or file)")
        if has_scraper_pull and (has_search_pull or has_feed_pull):
            raise ValueError("pull cannot mix scraper with feed or search items")
        if has_scraper_pull:
            seen_adapters: set[str] = set()
            for item in self.pull:
                if item.scraper is None:
                    continue
                adapter = item.scraper.adapter
                if adapter in seen_adapters:
                    raise ValueError(f"pull has duplicate scraper adapter: {adapter}")
                seen_adapters.add(adapter)
        if has_search_pull and has_feed_pull:
            raise ValueError("pull cannot mix feed and search items")
        if has_weather_pull and (has_feed_pull or has_search_pull or has_scraper_pull):
            raise ValueError("pull cannot mix weather with other pull types")
        if has_weather_pull and sum(1 for item in self.pull if item.weather is not None) > 1:
            raise ValueError("pull can have at most one weather item")
        if has_finance_pull and (
            has_feed_pull or has_search_pull or has_scraper_pull or has_weather_pull
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
    search: _GlobalSearch | None = None
    summarize: _GlobalSummarize | None = None
    youtube: _Youtube | None = None
    image: _Image | None = None
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
