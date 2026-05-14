import json
import pathlib
import re
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.functional_validators import AfterValidator, BeforeValidator

_PERIOD_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_color(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.startswith("#") and len(s) == 7:
        return int(s[1:], 16)
    return None


def parse_period(value) -> timedelta:
    s = str(value).strip() if not isinstance(value, str) else value.strip()
    if not s:
        raise ValueError("empty period")
    suffix = s[-1].lower()
    if suffix not in _PERIOD_UNITS:
        raise ValueError(f"missing suffix in {value!r} — use e.g. '30m', '6h', '1d'")
    return timedelta(**{_PERIOD_UNITS[suffix]: float(s[:-1])})


def task_kind(task_cfg: dict) -> str:
    explicit = task_cfg.get("kind")
    if explicit:
        return explicit
    pull = task_cfg.get("pull", [])
    if any("scraper" in item for item in pull):
        return "scraper"
    if any("search" in item for item in pull):
        return "search"
    return "feeds"


def get_feeds(task_cfg: dict) -> list[dict]:
    return [item["feed"] for item in task_cfg.get("pull", []) if "feed" in item]


def get_discord_cfg(task_cfg: dict) -> dict:
    return next((item["discord"] for item in task_cfg.get("push", []) if "discord" in item), {})


def get_search_cfg(task_cfg: dict) -> dict:
    return next((item["search"] for item in task_cfg.get("pull", []) if "search" in item), {})


def _get_scraper_cfg(task_cfg: dict) -> dict:
    return next((item["scraper"] for item in task_cfg.get("pull", []) if "scraper" in item), {})


def get_file_path(task_cfg: dict) -> str | None:
    return next((item["file"] for item in task_cfg.get("push", []) if "file" in item), None)


def _resolve_model_spec(spec) -> tuple[str | None, str | None]:
    """Return (provider, model_name) from a {provider: model_name} dict, or (None, None)."""
    if spec is None:
        return None, None
    provider, model = next(iter(spec.items()))
    return provider or None, model or None


def resolve_model_specs(spec) -> list[tuple[str | None, str | None]]:
    """Return list of (provider, model_name) from a single or list model spec."""
    if spec is None:
        return []
    if isinstance(spec, list):
        return [_resolve_model_spec(s) for s in spec]
    return [_resolve_model_spec(spec)]


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
            raise ValueError(f"invalid value {v!r} — expected e.g. '30m', '6h', '1d'")
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


_KNOWN_PROVIDERS = {"openai", "gemini", "deepseek", "anthropic"}


def _validate_model_spec(v) -> dict:
    if not isinstance(v, dict) or len(v) != 1:
        raise ValueError("model spec must be a single-key dict like {openai: gpt-4o-mini}")
    provider = next(iter(v))
    if provider not in _KNOWN_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r}; must be one of {sorted(_KNOWN_PROVIDERS)}"
        )
    return v


_ModelSpec = Annotated[dict, AfterValidator(_validate_model_spec)]


def _normalize_model_spec_list(v):
    if v is None or isinstance(v, list):
        return v
    return [v]


_ModelSpecList = Annotated[list[_ModelSpec] | None, BeforeValidator(_normalize_model_spec_list)]


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
    model: _ModelSpecList = None
    language: str | None = None


class _GlobalSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: _ModelSpecList = None
    instructions: str | None = None


class _GlobalSummarize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: _ModelSpecList = None


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
    model: _ModelSpec | None = None
    web_search: bool | dict
    instructions: str | None = None


class _PullScraperItem(BaseModel):
    model_config = ConfigDict(extra="allow")  # adapter-specific keys are allowed
    adapter: str
    url: str
    max_items: int | None = None


class _PullItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed: _PullFeedItem | None = None
    search: _PullSearchItem | None = None
    scraper: _PullScraperItem | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        present = [k for k in ("feed", "search", "scraper") if k in self.model_fields_set]
        if len(present) != 1:
            raise ValueError(
                "each pull item must have exactly one key: 'feed', 'search', or 'scraper'"
            )
        return self


class _PushDiscordItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhook: str
    color: _Color = None
    format: Literal["embed", "markdown"] | None = None


class _PushItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord: _PushDiscordItem | None = None
    file: str | None = None

    @model_validator(mode="after")
    def _has_target(self):
        if not self.model_fields_set:
            raise ValueError("each push item must have a target key (e.g. 'discord', 'file')")
        return self


class _TaskCurate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    model: _ModelSpec | None = None
    language: str | None = None
    instructions: str | None = None
    web_search: bool | dict | None = None
    explain: bool | None = None


class _Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["digest", "scraper"] | None = None
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
        has_discord_push = any(item.discord is not None for item in self.push)
        has_file_push = any(item.file is not None for item in self.push)
        if not (has_discord_push or has_file_push):
            raise ValueError("push must contain at least one target (discord or file)")
        if has_scraper_pull and (has_search_pull or has_feed_pull):
            raise ValueError("pull cannot mix scraper with feed or search items")
        if has_scraper_pull and sum(1 for item in self.pull if item.scraper is not None) > 1:
            raise ValueError("pull can have at most one scraper item")
        if has_search_pull and has_feed_pull:
            raise ValueError("pull cannot mix feed and search items")
        return self


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord: _GlobalDiscord | None = None
    feeds: _Feeds | None = None
    llm: _GlobalLLM | None = None
    curate: _GlobalCurate | None = None
    search: _GlobalSearch | None = None
    summarize: _GlobalSummarize | None = None
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
