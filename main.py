#!/usr/bin/env python3
"""Feed aggregator and notifier: RSS feeds, scrapers, and LLM tasks posted to Discord"""

import argparse
import asyncio
import fcntl
import logging
import os
import pathlib
import sys
import time
from datetime import UTC, datetime, timedelta

import aiohttp

from config import (
    get_api_key_for_provider,
    get_discord_cfg,
    get_feeds,
    get_llm_pull_cfg,
    load_config,
    parse_color,
    parse_period,
    resolve_model_specs,
    task_kind,
    validate_config,
)
from constants import USER_AGENT
from evals.capture import RunCapture
from process.summarize import run_summarize
from providers.llm import FallbackAdapter, get_adapter
from pull.feed import RSSSource
from state import _auto_clean, _remove_unknown, load_state, save_state
from state.migrate import CURRENT_VERSION, migrate, needs_migration
from tasks import (
    DEFAULT_PERIOD,
    _is_due,
    _process_llm_curate_task,
    _process_llm_search_task,
    _process_scraper_task,
)


def _under_systemd() -> bool:
    # JOURNAL_STREAM is set when stdout/stderr is captured by journald
    return "JOURNAL_STREAM" in os.environ


_SYSLOG_PREFIX = {
    logging.CRITICAL: "<2>",
    logging.ERROR: "<3>",
    logging.WARNING: "<4>",
    logging.INFO: "<6>",
    logging.DEBUG: "<7>",
}


class _JournaldFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _SYSLOG_PREFIX.get(record.levelno, "<6>") + super().format(record)


if _under_systemd():
    _handler = logging.StreamHandler()
    _handler.setFormatter(_JournaldFormatter("[%(name)s] %(message)s"))
    logging.basicConfig(handlers=[_handler], level=logging.INFO)
elif sys.stderr.isatty():
    from rich.logging import RichHandler

    logging.basicConfig(
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        level=logging.INFO,
    )
else:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
log = logging.getLogger(__name__)

_LOG_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)


def _setup_log_file(logs_dir: pathlib.Path, ts: datetime) -> None:
    if _under_systemd():
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y-%m-%dT%H-%M-%S")
    handler = logging.FileHandler(logs_dir / f"{stamp}.log", encoding="utf-8")
    handler.setFormatter(_LOG_FORMAT)
    logging.getLogger().addHandler(handler)


def _prune_old_files(root: pathlib.Path, days: int) -> int:
    if days <= 0 or not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def _check_due_or_skip(name: str, last_run: str | None, period: timedelta, now: datetime) -> bool:
    """True if the task is due. Otherwise log a skip message and return False."""
    if _is_due({"last_run": last_run}, period, now):
        return True
    if last_run:
        last = datetime.fromisoformat(last_run)
        mins = int((now - last).total_seconds() // 60)
        log.info("[%s] Skipping — last run %d min ago, period is %s", name, mins, period)
    return False


def _build_adapter(specs: list[tuple[str | None, str | None]], api_key_cfg: dict | None) -> tuple:
    """Return (adapter, model) from a list of (provider, model) specs.

    Single spec: returns a plain adapter + model string (preserves per-task model overrides).
    Multiple specs: returns a FallbackAdapter with bundled models + None.
    """
    valid = [(p, m) for p, m in specs if p]
    if not valid:
        return None, None
    if len(valid) == 1:
        p, m = valid[0]
        return get_adapter(p, get_api_key_for_provider(api_key_cfg, p)), m
    entries = [(get_adapter(p, get_api_key_for_provider(api_key_cfg, p)), m) for p, m in valid]
    return FallbackAdapter(entries), None


def _xdg_config_path() -> pathlib.Path:
    xdg_config = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_config / "claudinho" / "config.yaml"


def _xdg_state_path() -> pathlib.Path:
    xdg_data = pathlib.Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return xdg_data / "claudinho" / "state.json"


async def _async_main(args: argparse.Namespace) -> None:
    xdg_defaults = args.config is None
    config_path = (
        _xdg_config_path() if xdg_defaults else pathlib.Path(args.config).expanduser().resolve()
    )
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else (_xdg_state_path() if xdg_defaults else config_path.parent / "state.json")
    )

    _xdg_runtime = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/claudinho-{os.getuid()}"))
    _xdg_runtime.mkdir(parents=True, exist_ok=True)
    lock_path = _xdg_runtime / "claudinho.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        log.error("Another instance is running, exiting.")
        sys.exit(1)

    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    _run_ts = datetime.now(UTC).replace(microsecond=0)
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

    retention_days = (config.get("retention") or {}).get("days", 30)
    for sub in ("logs", "evals"):
        removed = _prune_old_files(state_path.parent / sub, retention_days)
        if removed:
            log.info(
                "Pruned %d file(s) older than %d day(s) from %s/", removed, retention_days, sub
            )

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
            t["name"]: {f["url"] for f in get_feeds(t) if f.get("url")}
            for t in config.get("tasks", [])
            if t.get("name") and t.get("pull")
        }
        _remove_unknown(state, known_tasks, known_feeds)
        save_state(state_path, state)
        log.info("Done. State saved to %s", state_path)
        return

    llm_cfg = config.get("llm", {})
    instructions = llm_cfg.get("instructions") or None
    api_key_cfg = llm_cfg.get("api_key") or None
    llm_models = llm_cfg.get("models") or {}
    evaluate_adapter, evaluate_model = _build_adapter(
        resolve_model_specs(llm_models.get("reasoning")), api_key_cfg
    )
    search_adapter, search_model = _build_adapter(
        resolve_model_specs(llm_models.get("topic")), api_key_cfg
    )
    global_language = llm_cfg.get("language") or "EN-US"
    discord_cfg = config.get("discord", {})
    global_color = parse_color(discord_cfg.get("color"))
    feeds_cfg = config.get("feeds") or {}
    max_age_seconds = int(feeds_cfg.get("max_age_days") or 7) * 86400

    tasks = config.get("tasks", [])
    if not tasks:
        log.error("No tasks defined in config.")
        sys.exit(1)

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": USER_AGENT},
    ) as session:
        if args.regenerate_state:
            now = datetime.now(UTC).replace(microsecond=0).isoformat()
            for task_cfg in tasks:
                if task_kind(task_cfg) != "feeds":
                    continue
                task_name = task_cfg.get("name")
                if not task_name:
                    log.warning("Skipping feeds task with no name")
                    continue
                task_state = state.setdefault("tasks", {}).setdefault(task_name, {})
                feeds_state = task_state.setdefault("feeds", {})
                source = RSSSource(max_age_seconds)
                for feed_cfg in get_feeds(task_cfg):
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
            now = datetime.now(UTC)
            feed_tasks = []
            force_task = args.task
            analysis = args.analysis
            collector = RunCapture(
                limit=args.analysis_limit_items if analysis else 0,
                limit_feeds=args.analysis_limit_feeds if analysis else 0,
            )

            task_list = tasks
            if force_task:
                task_list = [t for t in tasks if t.get("name") == force_task]
                if not task_list:
                    log.error("No task named %r found in config", force_task)
                    sys.exit(1)

            for task_cfg in task_list:
                webhook = get_discord_cfg(task_cfg).get("webhook")
                if not webhook:
                    continue
                period = parse_period(task_cfg.get("period", DEFAULT_PERIOD))
                kind = task_kind(task_cfg)
                name = task_cfg.get("name")
                if not name:
                    log.warning("Skipping %s task with no name", kind)
                    continue
                task_state = state.get("tasks", {}).get(name, {})

                if kind == "llm":
                    if not get_llm_pull_cfg(task_cfg).get("web_search"):
                        log.warning(
                            "[%s] Skipping LLM task: llm.web_search not configured (no input source)",
                            name,
                        )
                        continue
                    if (
                        not force_task
                        and not analysis
                        and not _check_due_or_skip(name, task_state.get("last_run"), period, now)
                    ):
                        continue
                    feed_tasks.append(
                        _process_llm_search_task(
                            task_cfg,
                            state,
                            session,
                            instructions=instructions,
                            search_model=search_model,
                            llm_adapter=search_adapter,
                            collector=collector,
                            analysis=analysis,
                        )
                    )
                elif kind == "scraper":
                    if analysis:
                        log.info("[%s] Skipping scraper task in analysis mode", name)
                        continue
                    if not force_task and not _check_due_or_skip(
                        name, task_state.get("last_run"), period, now
                    ):
                        continue
                    feed_tasks.append(
                        _process_scraper_task(task_cfg, state, session, global_color=global_color)
                    )
                else:
                    feeds_state = task_state.get("feeds", {})
                    feed_urls = [f["url"] for f in get_feeds(task_cfg) if f.get("url")]
                    if (
                        not force_task
                        and not analysis
                        and not any(_is_due(feeds_state.get(u, {}), period, now) for u in feed_urls)
                    ):
                        log.info("[%s] Skipping — no feeds are due", name)
                        continue
                    feed_tasks.append(
                        _process_llm_curate_task(
                            task_cfg,
                            state,
                            session,
                            evaluate_model=evaluate_model,
                            llm_adapter=evaluate_adapter,
                            global_color=global_color,
                            global_language=global_language,
                            max_age_seconds=max_age_seconds,
                            collector=collector,
                            analysis=analysis,
                        )
                    )

            results = await asyncio.gather(*feed_tasks, return_exceptions=True)

            evals_dir = state_path.parent / "evals"
            run_stamp = _run_ts.strftime("%Y-%m-%dT%H-%M-%S")
            written = collector.write_jsonl(evals_dir, run_stamp)
            if written:
                log.info("Wrote eval traces: %d file(s) under %s", len(written), evals_dir)

            if analysis:
                if args.human:
                    collector.display()
                else:
                    print(collector.to_json())
                return

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
    parser = argparse.ArgumentParser(
        description="Feed aggregator and notifier: RSS feeds, scrapers, and LLM tasks posted to Discord"
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to config file (YAML or JSON). Default: $XDG_CONFIG_HOME/claudinho/config.yaml",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="Path to state file. Default: $XDG_DATA_HOME/claudinho/state.json (or <config_dir>/state.json when config is given explicitly)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
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
    mode.add_argument(
        "--replay",
        metavar="JSONL",
        help="Replay captured LLM calls in JSONL against alternative models (requires --models)",
    )
    parser.add_argument(
        "--models",
        metavar="LIST",
        help="Comma-separated provider:model pairs to replay against, e.g. openai:gpt-4o-mini,gemini:gemini-2.5-flash",
    )
    parser.add_argument(
        "--call",
        choices=["filter", "summarize", "llm_search"],
        help="With --replay: only re-issue calls of this type (default: all)",
    )
    parser.add_argument(
        "--analysis",
        action="store_true",
        help="Inspection mode: enables LLM reasoning + ELI5 filter reasons, dry-run (no posting / no state update), renders to stdout. Costs more tokens. Typically combined with --task.",
    )
    parser.add_argument(
        "--analysis-limit-items",
        type=int,
        default=7,
        metavar="N",
        help="Analysis mode: max entries per feed to process (default: 7, newest first). 0 = unlimited.",
    )
    parser.add_argument(
        "--analysis-limit-feeds",
        type=int,
        default=7,
        metavar="N",
        help="Analysis mode: max feeds per task to process (default: 7). 0 = unlimited.",
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="With --analysis: render rich/human-readable output to stdout instead of JSON.",
    )
    args = parser.parse_args()

    if args.verbose:
        for name in (
            "__main__",
            "pull.feed",
            "pull.llm",
            "push.discord",
            "pull.scraper",
            "pull.scrapers.vivareal",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)

    if args.validate:
        config_path = (
            _xdg_config_path()
            if args.config is None
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

    if args.replay:
        if not args.models:
            log.error("--replay requires --models")
            sys.exit(1)
        from evals.replay import replay as _replay

        jsonl_path = pathlib.Path(args.replay).expanduser().resolve()
        config_path = (
            _xdg_config_path()
            if args.config is None
            else pathlib.Path(args.config).expanduser().resolve()
        )
        if not config_path.exists():
            log.error("Config file not found: %s", config_path)
            sys.exit(1)
        state_path = (
            pathlib.Path(args.state).expanduser().resolve()
            if args.state
            else (_xdg_state_path() if args.config is None else config_path.parent / "state.json")
        )
        model_specs = [s.strip() for s in args.models.split(",") if s.strip()]
        asyncio.run(
            _replay(
                jsonl_path,
                model_specs,
                args.call,
                state_path.parent,
                config_path,
            )
        )
        return

    if args.summarize:
        _sum_adapter = None
        _sum_api_key_cfg = None
        _sum_model = None
        _sum_language = "EN-US"
        config_path = (
            _xdg_config_path()
            if args.config is None
            else pathlib.Path(args.config).expanduser().resolve()
        )
        if config_path.exists():
            try:
                cfg = load_config(config_path)
                llm_cfg = cfg.get("llm") or {}
                _sum_api_key_cfg = llm_cfg.get("api_key") or None
                _sum_language = llm_cfg.get("language") or "EN-US"
                _sum_specs = resolve_model_specs((llm_cfg.get("models") or {}).get("topic"))
                _sum_adapter, _sum_model = _build_adapter(_sum_specs, _sum_api_key_cfg)
            except Exception:
                pass
        if _sum_adapter is None:
            log.error("--summarize requires llm.models.topic with a provider configured")
            sys.exit(1)
        asyncio.run(
            run_summarize(
                args.summarize, adapter=_sum_adapter, model=_sum_model, language=_sum_language
            )
        )
        return

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
