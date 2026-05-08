import json
import re
import pathlib
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic import field_validator, model_validator
from pydantic.functional_validators import BeforeValidator, AfterValidator


_PERIOD_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _parse_color(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.startswith("#") and len(s) == 7:
        return int(s[1:], 16)
    return None


def _parse_period(value) -> timedelta:
    s = str(value).strip() if not isinstance(value, str) else value.strip()
    if not s:
        raise ValueError("empty period")
    suffix = s[-1].lower()
    if suffix not in _PERIOD_UNITS:
        raise ValueError(f"missing suffix in {value!r} — use e.g. '30m', '6h', '1d'")
    return timedelta(**{_PERIOD_UNITS[suffix]: float(s[:-1])})


def _task_type(task_cfg: dict) -> str:
    explicit = task_cfg.get("type")
    if explicit:
        return explicit
    return "llm" if "feeds" not in task_cfg and "llm" in task_cfg else "feeds"


def load_config(path: pathlib.Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


# --- Annotated constraint types ---

def _color_validator(v):
    result = _parse_color(v)
    if v is not None and result is None:
        raise ValueError(f"invalid color {v!r} — expected '#RRGGBB'")
    return result


def _period_validator(v):
    if v is not None:
        try:
            _parse_period(v)
        except (ValueError, TypeError):
            raise ValueError(
                f"invalid value {v!r} — expected e.g. '30m', '6h', '1d'"
            )
    return v


_Color = Annotated[int | None, BeforeValidator(_color_validator)]
_Period = Annotated[str | None, AfterValidator(_period_validator)]


# --- Models ---

class _GlobalDiscord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    color: _Color = None


class _GlobalLLM(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str | None = None
    language: str | None = None
    api_key: str | None = None
    instructions: str | None = None


class _FilterOp(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    remove_phrases_with_urls: Any = None
    remove_phrases_containing: Any = None
    extract: str | None = None
    replace: str | None = None
    with_: str | None = Field(None, alias='with')
    clear: bool | None = None

    @model_validator(mode='after')
    def _has_op(self):
        ops = {'remove_phrases_with_urls', 'remove_phrases_containing', 'extract', 'replace', 'clear'}
        if not (ops & self.model_fields_set):
            raise ValueError("no recognized operation key")
        return self

    @field_validator('extract', 'replace', mode='before')
    @classmethod
    def _valid_regex(cls, v):
        if isinstance(v, str):
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regex {v!r}: {exc}")
        return v


class _UrlFilter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    skip_containing: str | list[str] | None = None

    @model_validator(mode='after')
    def _has_op(self):
        if not self.model_fields_set:
            raise ValueError("no recognized operation key")
        return self


class _FilterDict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: list[_FilterOp] | _FilterOp | None = None
    description: list[_FilterOp] | _FilterOp | None = None
    url: _UrlFilter | None = None


class _FeedDiscord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    color: _Color = None


class _OgImage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    skip: bool | None = None
    download: bool | None = None


class _Feed(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = None
    url: str
    discord: _FeedDiscord = Field(default_factory=_FeedDiscord)
    og_image: _OgImage | None = None
    filter: _FilterDict | None = None


class _TaskDiscord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    webhook: str
    color: _Color = None


class _TaskLLM(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str
    model: str | None = None
    language: str | None = None
    web_search: bool | dict | None = None
    explain: bool | None = None


class _Task(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    type: Literal['feeds', 'llm', 'digest'] | None = None
    discord: _TaskDiscord
    period: _Period = None
    og_image: _OgImage | None = None
    filter: _FilterDict | None = None
    llm: _TaskLLM | None = None
    feeds: list[_Feed] = []

    @model_validator(mode='after')
    def _check_llm_task(self):
        task_type = self.type or (
            "llm" if "feeds" not in self.model_fields_set and self.llm is not None else "feeds"
        )
        if task_type == "llm":
            if self.llm is None:
                raise ValueError("LLM task (no 'feeds' key) requires an 'llm' key")
            if not self.llm.web_search:
                raise ValueError("'web_search' is required for LLM tasks (no 'feeds' configured)")
        return self


class _Config(BaseModel):
    model_config = ConfigDict(extra='forbid')
    discord: _GlobalDiscord | None = None
    llm: _GlobalLLM | None = None
    og_image: _OgImage | None = None
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
        return [f"{_fmt_loc(e['loc'])}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors()]
