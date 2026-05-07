import json
import re
import pathlib
from datetime import timedelta


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
    if isinstance(value, str):
        value = value.strip()
        suffix = value[-1].lower() if value else ""
        if suffix in _PERIOD_UNITS:
            return timedelta(**{_PERIOD_UNITS[suffix]: float(value[:-1])})
        return timedelta(hours=float(value))
    return timedelta(hours=float(value))


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


_VALID_CONFIG_KEYS = {"discord", "llm", "og_image_download", "tasks"}
_VALID_GLOBAL_DISCORD_KEYS = {"color"}
_VALID_GLOBAL_LLM_KEYS = {"model", "language", "api_key", "instructions"}
_VALID_TASK_KEYS = {"name", "type", "discord", "period", "og_image", "og_image_download", "filter", "llm", "feeds"}
_VALID_TASK_DISCORD_KEYS = {"webhook", "color"}
_VALID_TASK_LLM_KEYS = {"prompt", "model", "language", "web_search", "explain"}
_VALID_FEED_KEYS = {"name", "url", "discord", "og_image_download", "filter"}
_VALID_FEED_DISCORD_KEYS = {"color"}
_VALID_FILTER_KEYS = {"title", "description"}
_VALID_FILTER_OPS = {"remove_phrases_with_urls", "remove_phrases_containing", "extract", "replace", "with", "clear"}
_VALID_TASK_TYPES = {"feeds", "llm", "digest"}


def _check_keys(d: dict, valid: set, path: str) -> list[str]:
    errors = []
    for k in sorted(set(d) - valid):
        if k.replace("-", "_") in valid:
            errors.append(f"{path}: unknown key {k!r} — did you mean '{k.replace('-', '_')}'?")
        else:
            errors.append(f"{path}: unknown key {k!r}")
    return errors


def _validate_color(value, path: str) -> list[str]:
    if value is None or isinstance(value, int):
        return []
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("#") and len(s) == 7:
            try:
                int(s[1:], 16)
                return []
            except ValueError:
                pass
    return [f"{path}: invalid color {value!r} — expected '#RRGGBB'"]


def _validate_filter_op(op, path: str) -> list[str]:
    if not isinstance(op, dict):
        return [f"{path}: expected a dict, got {type(op).__name__}"]
    errors = _check_keys(op, _VALID_FILTER_OPS, path)
    if not any(k in op for k in ("remove_phrases_with_urls", "remove_phrases_containing", "extract", "replace", "clear")):
        errors.append(f"{path}: no recognized operation key")
    for regex_key in ("extract", "replace"):
        if regex_key in op and isinstance(op[regex_key], str):
            try:
                re.compile(op[regex_key])
            except re.error as exc:
                errors.append(f"{path}.{regex_key}: invalid regex {op[regex_key]!r}: {exc}")
    return errors


def _validate_filter(cfg, path: str) -> list[str]:
    if isinstance(cfg, list):
        errors = []
        for i, item in enumerate(cfg):
            errors.extend(_validate_filter_op(item, f"{path}[{i}]"))
        return errors
    return _validate_filter_op(cfg, path)


def _validate_filter_dict(cfg, path: str) -> list[str]:
    if not isinstance(cfg, dict):
        return [f"{path}: expected a dict, got {type(cfg).__name__}"]
    errors = _check_keys(cfg, _VALID_FILTER_KEYS, path)
    for field in ("title", "description"):
        if field in cfg:
            errors.extend(_validate_filter(cfg[field], f"{path}.{field}"))
    return errors


def validate_config(config: dict) -> list[str]:
    errors = _check_keys(config, _VALID_CONFIG_KEYS, "config")

    gd = config.get("discord")
    if gd is not None:
        if not isinstance(gd, dict):
            errors.append("config.discord: expected a dict")
        else:
            errors.extend(_check_keys(gd, _VALID_GLOBAL_DISCORD_KEYS, "config.discord"))
            errors.extend(_validate_color(gd.get("color"), "config.discord.color"))

    gl = config.get("llm")
    if gl is not None:
        if not isinstance(gl, dict):
            errors.append("config.llm: expected a dict")
        else:
            errors.extend(_check_keys(gl, _VALID_GLOBAL_LLM_KEYS, "config.llm"))

    if "og_image_download" in config and not isinstance(config["og_image_download"], bool):
        errors.append(f"config.og_image_download: expected bool, got {type(config['og_image_download']).__name__}")

    tasks = config.get("tasks")
    if not isinstance(tasks, list):
        errors.append("config.tasks: expected a list")
        return errors

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"config.tasks[{i}]: expected a dict")
            continue

        name = task.get("name") or f"<task #{i}>"
        p = f"task '{name}'"

        errors.extend(_check_keys(task, _VALID_TASK_KEYS, p))

        if not task.get("name"):
            errors.append(f"{p}: missing required key 'name'")

        td = task.get("discord")
        if td is None:
            errors.append(f"{p}: missing required key 'discord'")
        elif not isinstance(td, dict):
            errors.append(f"{p}.discord: expected a dict")
        else:
            errors.extend(_check_keys(td, _VALID_TASK_DISCORD_KEYS, f"{p}.discord"))
            if not td.get("webhook"):
                errors.append(f"{p}.discord: missing required key 'webhook'")
            errors.extend(_validate_color(td.get("color"), f"{p}.discord.color"))

        if "type" in task and task["type"] not in _VALID_TASK_TYPES:
            errors.append(f"{p}.type: invalid value {task['type']!r} — expected one of {sorted(_VALID_TASK_TYPES)}")

        if "period" in task:
            try:
                _parse_period(task["period"])
            except (ValueError, TypeError):
                errors.append(f"{p}.period: invalid value {task['period']!r} — expected a number (hours) or e.g. '30m', '6h', '1d'")

        for bool_key in ("og_image", "og_image_download"):
            if bool_key in task and not isinstance(task[bool_key], bool):
                errors.append(f"{p}.{bool_key}: expected bool, got {type(task[bool_key]).__name__}")

        task_type = _task_type(task)

        llm = task.get("llm")
        if llm is not None:
            if not isinstance(llm, dict):
                errors.append(f"{p}.llm: expected a dict")
            else:
                errors.extend(_check_keys(llm, _VALID_TASK_LLM_KEYS, f"{p}.llm"))
                if not llm.get("prompt"):
                    errors.append(f"{p}.llm: missing required key 'prompt'")
                for str_key in ("model", "language"):
                    if str_key in llm and not isinstance(llm[str_key], str):
                        errors.append(f"{p}.llm.{str_key}: expected str")
                if "explain" in llm and not isinstance(llm["explain"], bool):
                    errors.append(f"{p}.llm.explain: expected bool")
                ws = llm.get("web_search")
                if ws is not None and not isinstance(ws, (bool, dict)):
                    errors.append(f"{p}.llm.web_search: expected bool or dict")
                if task_type == "llm" and not ws:
                    errors.append(f"{p}.llm: 'web_search' is required for LLM tasks (no 'feeds' configured)")
        elif task_type == "llm":
            errors.append(f"{p}: LLM task (no 'feeds' key) requires an 'llm' key")

        if "filter" in task:
            errors.extend(_validate_filter_dict(task["filter"], f"{p}.filter"))

        if task_type in ("feeds", "digest"):
            feeds_list = task.get("feeds", [])
            if not isinstance(feeds_list, list):
                errors.append(f"{p}.feeds: expected a list")
            else:
                for j, feed in enumerate(feeds_list):
                    if not isinstance(feed, dict):
                        errors.append(f"{p}.feeds[{j}]: expected a dict")
                        continue
                    flabel = feed.get("name") or feed.get("url") or f"<feed #{j}>"
                    fp = f"{p} feed '{flabel}'"
                    errors.extend(_check_keys(feed, _VALID_FEED_KEYS, fp))
                    if not feed.get("url"):
                        errors.append(f"{fp}: missing required key 'url'")
                    fd = feed.get("discord", {})
                    if not isinstance(fd, dict):
                        errors.append(f"{fp}.discord: expected a dict")
                    else:
                        errors.extend(_check_keys(fd, _VALID_FEED_DISCORD_KEYS, f"{fp}.discord"))
                        errors.extend(_validate_color(fd.get("color"), f"{fp}.discord.color"))
                    if "og_image_download" in feed and not isinstance(feed["og_image_download"], bool):
                        errors.append(f"{fp}.og_image_download: expected bool")
                    if "filter" in feed:
                        errors.extend(_validate_filter_dict(feed["filter"], f"{fp}.filter"))

    return errors
