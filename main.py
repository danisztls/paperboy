#!/usr/bin/env python3
"""RSS to Discord webhook notifier"""

import asyncio
import atexit
import logging
import os
import pathlib
import re
import sys
import argparse
from datetime import datetime, timezone

import aiohttp

from config import _parse_color, _parse_period, _task_type, _get_feeds, _get_discord_cfg, _get_llm_pull_cfg, load_config, validate_config
from state import _auto_clean, _remove_unknown, load_state, save_state
from tasks import DEFAULT_PERIOD, _is_due, _process_llm_search_task, _process_llm_evaluate_task
from feed import RSSSource
from llm import summarize_transcript
from migrate import CURRENT_VERSION, needs_migration, migrate

def _under_systemd() -> bool:
    # JOURNAL_STREAM is set when stdout/stderr is captured by journald
    return "JOURNAL_STREAM" in os.environ


_SYSLOG_PREFIX = {
    logging.CRITICAL: "<2>",
    logging.ERROR:    "<3>",
    logging.WARNING:  "<4>",
    logging.INFO:     "<6>",
    logging.DEBUG:    "<7>",
}


class _JournaldFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _SYSLOG_PREFIX.get(record.levelno, "<6>") + super().format(record)


if _under_systemd():
    _handler = logging.StreamHandler()
    _handler.setFormatter(_JournaldFormatter("[%(name)s] %(message)s"))
    logging.basicConfig(handlers=[_handler], level=logging.INFO)
else:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
log = logging.getLogger(__name__)

_LOG_FORMAT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def _setup_log_file(logs_dir: pathlib.Path, ts: datetime) -> None:
    if _under_systemd():
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y-%m-%dT%H-%M-%S")
    handler = logging.FileHandler(logs_dir / f"{stamp}.log", encoding="utf-8")
    handler.setFormatter(_LOG_FORMAT)
    logging.getLogger().addHandler(handler)


def _xdg_config_path() -> pathlib.Path:
    xdg_config = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_config / "claudinho" / "config.yaml"


def _xdg_state_path() -> pathlib.Path:
    xdg_data = pathlib.Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return xdg_data / "claudinho" / "state.json"


_VTT_TIMESTAMP_RE = re.compile(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->')
_VTT_TAGS_RE = re.compile(r'<[^>]+>')
_VTT_META_RE = re.compile(r'^(WEBVTT|Kind:|Language:)')


def _parse_vtt(content: str) -> str:
    """Extract clean text from a WebVTT subtitle file.

    Strips cue timestamps, HTML tags, numeric identifiers, and position
    metadata, then deduplicates consecutive identical lines (YouTube
    auto-captions emit overlapping cues with the same text).
    """
    lines = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _VTT_META_RE.match(line) or _VTT_TIMESTAMP_RE.match(line):
            continue
        if line.isdigit():
            continue
        line = _VTT_TAGS_RE.sub('', line).strip()
        if line:
            lines.append(line)
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return ' '.join(deduped)


async def _async_summarize(url: str, api_key: str | None, model: str | None, language: str = "EN-US") -> None:
    try:
        import yt_dlp
    except ImportError:
        log.error("yt-dlp is not installed — run: uv sync")
        sys.exit(1)

    def _extract():
        with yt_dlp.YoutubeDL({'skip_download': True, 'quiet': True, 'no_warnings': True}) as ydl:
            return ydl.extract_info(url, download=False)

    log.info("Fetching video info: %s", url)
    try:
        info = await asyncio.to_thread(_extract)
    except Exception as exc:
        log.error("yt-dlp failed: %s", exc)
        sys.exit(1)

    title = info.get('title', '')
    subs = info.get('subtitles') or {}
    auto = info.get('automatic_captions') or {}

    lang_track = None
    for src in (subs, auto):
        for lang in ('en', 'en-orig', *src.keys()):
            if lang in src:
                lang_track = src[lang]
                break
        if lang_track:
            break

    if not lang_track:
        log.error("No subtitles or captions found for this video")
        sys.exit(1)

    vtt_entry = next((e for e in lang_track if e.get('ext') == 'vtt'), lang_track[0])
    sub_url = vtt_entry.get('url')
    if not sub_url:
        log.error("No subtitle URL found")
        sys.exit(1)

    log.info("Fetching captions for %r", title)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
    ) as session:
        try:
            async with session.get(sub_url) as resp:
                vtt_content = await resp.text()
        except aiohttp.ClientError as exc:
            log.error("Failed to fetch captions: %s", exc)
            sys.exit(1)

    transcript = _parse_vtt(vtt_content)
    if not transcript:
        log.error("No text could be extracted from captions")
        sys.exit(1)

    log.info("Transcript length: %d chars", len(transcript))
    summary = await summarize_transcript(title, transcript, api_key=api_key, model=model, language=language)
    if summary:
        print(summary)
    else:
        log.error("Summarization failed")
        sys.exit(1)


async def _async_main(args: argparse.Namespace) -> None:
    xdg_defaults = args.config is None
    config_path = (
        _xdg_config_path() if xdg_defaults
        else pathlib.Path(args.config).expanduser().resolve()
    )
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else (_xdg_state_path() if xdg_defaults else config_path.parent / "state.json")
    )

    _xdg_runtime = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/claudinho-{os.getuid()}"))
    lock_path = _xdg_runtime / "claudinho.lock"
    if lock_path.exists():
        raw = lock_path.read_text().strip()
        try:
            os.kill(int(raw), 0)
        except (ValueError, ProcessLookupError):
            log.warning("Removing stale lock file (PID %s)", raw)
        else:
            log.error("Another instance is running (PID %s), exiting.", raw)
            sys.exit(1)
    lock_path.write_text(str(os.getpid()))
    atexit.register(lock_path.unlink, missing_ok=True)

    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    _run_ts = datetime.now(timezone.utc).replace(microsecond=0)
    _setup_log_file(state_path.parent / "logs", _run_ts)

    log.info("Config: %s", config_path)
    log.info("State:  %s", state_path)

    config = load_config(config_path)
    config_errors = validate_config(config, config_path)
    if config_errors:
        for err in config_errors:
            log.error("Config error: %s", err)
        sys.exit(1)
    state = load_state(state_path)

    if args.migrate:
        if not needs_migration(state):
            log.info("State is already at version %d, nothing to do.", CURRENT_VERSION)
            return
        state = migrate(state)
        save_state(state_path, state)
        log.info("Migrated to v%d, saved to %s", CURRENT_VERSION, state_path)
        return

    if args.clean:
        _auto_clean(state)
        known_tasks = {t["name"] for t in config.get("tasks", []) if t.get("name")}
        known_feeds = {
            t["name"]: {f["url"] for f in _get_feeds(t) if f.get("url")}
            for t in config.get("tasks", [])
            if t.get("name") and t.get("pull")
        }
        _remove_unknown(state, known_tasks, known_feeds)
        save_state(state_path, state)
        log.info("Done. State saved to %s", state_path)
        return

    llm_cfg = config.get("llm", {})
    instructions = llm_cfg.get("instructions") or None
    llm_models = llm_cfg.get("models") or {}
    evaluate_model = llm_models.get("reasoning") or None
    search_model = llm_models.get("topic") or None
    llm_api_key = llm_cfg.get("api_key") or None
    global_language = llm_cfg.get("language") or "EN-US"
    discord_cfg = config.get("discord", {})
    global_color = _parse_color(discord_cfg.get("color"))
    global_og_download: bool = (config.get("og_image") or {}).get("download", False)

    tasks = config.get("tasks", [])
    if not tasks:
        log.error("No tasks defined in config.")
        sys.exit(1)

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
    ) as session:
        if args.regenerate_state:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            for task_cfg in tasks:
                if _task_type(task_cfg) != "feeds":
                    continue
                task_name = task_cfg.get("name")
                if not task_name:
                    log.warning("Skipping feeds task with no name")
                    continue
                task_state = state.setdefault("tasks", {}).setdefault(task_name, {})
                feeds_state = task_state.setdefault("feeds", {})
                source = RSSSource()
                for feed_cfg in _get_feeds(task_cfg):
                    url = feed_cfg.get("url")
                    if not url:
                        continue
                    pull_result = await source.pull(feed_cfg, set(), session)
                    if pull_result is None:
                        log.warning("Failed to fetch %s, skipping", url)
                        continue
                    prev_access = {
                        item["url"]: item["access_date"]
                        for item in feeds_state.get(url, {}).get("items", [])
                        if "access_date" in item
                    }
                    for item in pull_result.current_items:
                        item["access_date"] = prev_access.get(item["url"], now)
                    feeds_state[url] = {"items": pull_result.current_items, "last_run": now}
                    log.info("Regenerated %d items for %s", len(pull_result.current_items), url)
            save_state(state_path, state)
            log.info("Done. State regenerated and saved to %s", state_path)
        else:
            # Concurrent: all tasks run in parallel.
            now = datetime.now(timezone.utc)
            feed_tasks = []
            force_task = args.task

            task_list = tasks
            if force_task:
                task_list = [t for t in tasks if t.get("name") == force_task]
                if not task_list:
                    log.error("No task named %r found in config", force_task)
                    sys.exit(1)

            for task_cfg in task_list:
                webhook = _get_discord_cfg(task_cfg).get("webhook")
                if not webhook:
                    continue
                period = _parse_period(task_cfg.get("period", DEFAULT_PERIOD))
                if _task_type(task_cfg) == "llm":
                    name = task_cfg.get("name")
                    if not name:
                        log.warning("Skipping LLM task with no name")
                        continue
                    if not _get_llm_pull_cfg(task_cfg).get("web_search"):
                        log.warning("[%s] Skipping LLM task: llm.web_search not configured (no input source)", name)
                        continue
                    task_state = state.get("tasks", {}).get(name, {"last_run": None})
                    if not force_task and not _is_due(task_state, period, now):
                        last = datetime.fromisoformat(task_state["last_run"])
                        mins = int((now - last).total_seconds() // 60)
                        log.info(
                            "[%s] Skipping — last run %d min ago, period is %s",
                            name, mins, period,
                        )
                        continue
                    feed_tasks.append(_process_llm_search_task(task_cfg, state, session, instructions=instructions, search_model=search_model, llm_api_key=llm_api_key))
                else:
                    task_name = task_cfg.get("name")
                    if not task_name:
                        log.warning("Skipping feeds task with no name")
                        continue
                    feeds_state = state.get("tasks", {}).get(task_name, {}).get("feeds", {})
                    feed_urls = [f["url"] for f in _get_feeds(task_cfg) if f.get("url")]
                    if not force_task and not any(
                        _is_due(feeds_state.get(u, {"last_run": None}), period, now) for u in feed_urls
                    ):
                        log.info("[%s] Skipping — no feeds are due", task_name)
                        continue
                    feed_tasks.append(_process_llm_evaluate_task(task_cfg, state, session, evaluate_model=evaluate_model, llm_api_key=llm_api_key, global_color=global_color, global_language=global_language, global_og_download=global_og_download))

            results = await asyncio.gather(*feed_tasks, return_exceptions=True)
            tasks_state = state.setdefault("tasks", {})
            for result in results:
                if isinstance(result, Exception):
                    log.error("Feed task failed: %s", result)
                elif result:
                    for task_name, task_state in result.items():
                        if task_name in tasks_state:
                            tasks_state[task_name] = {**tasks_state[task_name], **task_state}
                        else:
                            tasks_state[task_name] = task_state
            save_state(state_path, state)
            log.info("Done. State saved to %s", state_path)


def main():
    parser = argparse.ArgumentParser(description="RSS to Discord webhook notifier")
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config file (YAML or JSON). Default: $XDG_CONFIG_HOME/claudinho/config.yaml",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="Path to state file. Default: $XDG_DATA_HOME/claudinho/state.json (or <config_dir>/state.json when config is given explicitly)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--task",
        metavar="NAME",
        help="Run a single task by name, ignoring period and last_run state",
    )
    mode.add_argument(
        "--regenerate-state",
        action="store_true",
        help="Fetch all feeds and write current items to state without posting to Discord",
    )
    mode.add_argument(
        "--clean",
        action="store_true",
        help="Remove state entries older than 30 days or missing access_date, then exit",
    )
    mode.add_argument(
        "--migrate",
        action="store_true",
        help="Migrate state.json to the current schema version, then exit",
    )
    mode.add_argument(
        "--validate",
        action="store_true",
        help="Validate the config file and exit",
    )
    mode.add_argument(
        "--summarize",
        metavar="URL",
        help="Fetch transcript from a YouTube video and print a summary to stdout",
    )
    args = parser.parse_args()

    if args.verbose:
        for name in ("__main__", "feed", "llm", "discord"):
            logging.getLogger(name).setLevel(logging.DEBUG)

    if args.validate:
        config_path = (
            _xdg_config_path() if args.config is None
            else pathlib.Path(args.config).expanduser().resolve()
        )
        if not config_path.exists():
            log.error("Config file not found: %s", config_path)
            sys.exit(1)
        config = load_config(config_path)
        errors = validate_config(config, config_path)
        if errors:
            for err in errors:
                log.error("Config error: %s", err)
            sys.exit(1)
        log.info("Config is valid: %s", config_path)
        return

    if args.summarize:
        api_key = None
        model = None
        language = "EN-US"
        config_path = (
            _xdg_config_path() if args.config is None
            else pathlib.Path(args.config).expanduser().resolve()
        )
        if config_path.exists():
            try:
                cfg = load_config(config_path)
                llm_cfg = cfg.get("llm") or {}
                api_key = llm_cfg.get("api_key") or None
                language = llm_cfg.get("language") or "EN-US"
                models_cfg = llm_cfg.get("models") or {}
                model = models_cfg.get("topic") or None
            except Exception:
                pass
        asyncio.run(_async_summarize(args.summarize, api_key=api_key, model=model, language=language))
        return

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
