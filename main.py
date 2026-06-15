#!/usr/bin/env python3
"""Feed aggregator and notifier: RSS feeds, real-estate listings, and LLM tasks posted to Discord"""

import argparse
import asyncio
import fcntl
import logging
import os
import pathlib
import sys
import time
from datetime import UTC, datetime

import aiohttp

import logsetup
from config import (
    get_discord_cfg,
    load_config,
    parse_period,
    resolve_model_specs,
    task_kind,
    validate_config,
)
from constants import USER_AGENT
from evals.capture import RunCapture
from process._vasco import configure as configure_vasco
from process.summarize import run_get_content, run_summarize
from providers.llm import build_model_handle
from state import auto_clean, load_state, remove_unknown, save_state
from state.migrate import CURRENT_VERSION, migrate, needs_migration
from stats import humanize_minutes, print_stats
from tasks import (
    DEFAULT_PERIOD,
    LLMHandles,
    RunContext,
    processor_for,
    regenerate_feeds_state,
    task_is_due,
)

log = logging.getLogger(__name__)


# --- Paths and process lock ---


def _xdg_config_path() -> pathlib.Path:
    xdg_config = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_config / "claudinho" / "config.yaml"


def _xdg_state_path() -> pathlib.Path:
    xdg_data = pathlib.Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return xdg_data / "claudinho" / "state.json"


def resolve_paths(args: argparse.Namespace) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve (config_path, state_path) from CLI args / XDG defaults.

    Exits when the config file does not exist. With an explicit --config but no
    --state, state lives next to the config file.
    """
    xdg_defaults = args.config is None
    config_path = (
        _xdg_config_path() if xdg_defaults else pathlib.Path(args.config).expanduser().resolve()
    )
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else (_xdg_state_path() if xdg_defaults else config_path.parent / "state.json")
    )
    return config_path, state_path


def _acquire_lock() -> int:
    """Take the single-instance flock; exits if another instance holds it."""
    xdg_runtime = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/claudinho-{os.getuid()}"))
    xdg_runtime.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(xdg_runtime / "claudinho.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        log.error("Another instance is running, exiting.")
        sys.exit(1)
    return lock_fd


def _load_valid_config(config_path: pathlib.Path) -> dict:
    config = load_config(config_path)
    errors = validate_config(config)
    if errors:
        for err in errors:
            log.error("Config error: %s", err)
        sys.exit(1)
    return config


# --- Housekeeping ---


def prune_old_files(root: pathlib.Path, days: int) -> int:
    if days <= 0 or not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def merge_task_results(state: dict, results: list) -> None:
    """Fold per-task result dicts into state. Empty/Exception results leave state untouched."""
    tasks_state = state.setdefault("tasks", {})
    for result in results:
        if isinstance(result, Exception):
            log.error("Task failed: %s", result)
        elif result:
            for task_name, task_state in result.items():
                if task_name in tasks_state:
                    tasks_state[task_name] = {**tasks_state[task_name], **task_state}
                else:
                    tasks_state[task_name] = task_state


# --- The normal run path ---


def _build_llm_handles(config: dict) -> LLMHandles:
    api_keys = (config.get("llm", {}).get("api_key")) or None

    def _handle(section: str):
        specs = resolve_model_specs((config.get(section) or {}).get("model"))
        return build_model_handle(specs, api_keys)

    return LLMHandles(
        curate=_handle("curate"),
        summarize=_handle("summarize"),
        research=_handle("research"),
    )


def _log_not_due(name: str, task_state: dict, period, now: datetime) -> None:
    last_run = task_state.get("last_run")
    if last_run:
        mins = int((now - datetime.fromisoformat(last_run)).total_seconds() // 60)
        log.debug(
            "[%s] Skipping — last run %s ago, period is %s", name, humanize_minutes(mins), period
        )
    else:
        log.debug("[%s] Skipping — no feeds are due (period %s)", name, period)


def _collect_due_tasks(ctx: RunContext, state: dict, force_task: str | None, now: datetime) -> list:
    """Build the processor coroutines for every due (or forced) task."""
    tasks_cfg = ctx.config.get("tasks", [])
    if force_task:
        tasks_cfg = [t for t in tasks_cfg if t.get("name") == force_task]
        if not tasks_cfg:
            log.error("No task named %r found in config", force_task)
            sys.exit(1)

    coros = []
    for task_cfg in tasks_cfg:
        if not get_discord_cfg(task_cfg).get("webhook"):
            continue
        kind = task_kind(task_cfg)
        name = task_cfg.get("name")
        if not name:
            log.warning("Skipping %s task with no name", kind)
            continue
        if kind == "realestate" and ctx.analysis:
            log.info("[%s] Skipping real-estate task in analysis mode", name)
            continue
        period = parse_period(task_cfg.get("period", DEFAULT_PERIOD))
        task_state = state.get("tasks", {}).get(name, {})
        if (
            not force_task
            and not ctx.analysis
            and not task_is_due(task_cfg, task_state, period, now)
        ):
            _log_not_due(name, task_state, period, now)
            continue
        coros.append(processor_for(kind)(task_cfg, state, ctx))
    return coros


async def _async_main(args: argparse.Namespace) -> None:
    config_path, state_path = resolve_paths(args)
    _lock_fd = _acquire_lock()  # held for the lifetime of the run

    run_local = datetime.now(UTC).replace(microsecond=0).astimezone()
    logsetup.add_file_handler(state_path.parent / "logs", run_local)

    log.info("Config: %s", config_path)
    log.info("State:  %s", state_path)

    config = _load_valid_config(config_path)
    state = load_state(state_path)

    if state and needs_migration(state):
        old_version = state.get("_version", 0)
        state = migrate(state, config)
        save_state(state_path, state)
        log.info("Auto-migrated state v%d → v%d", old_version, CURRENT_VERSION)

    retention_days = (config.get("retention") or {}).get("days", 30)
    for sub in ("logs", "evals"):
        removed = prune_old_files(state_path.parent / sub, retention_days)
        if removed:
            log.info(
                "Pruned %d file(s) older than %d day(s) from %s/", removed, retention_days, sub
            )

    if args.migrate:
        if not needs_migration(state):
            log.info("State is already at version %d, nothing to do.", CURRENT_VERSION)
            return
        state = migrate(state, config)
        save_state(state_path, state)
        log.info("Migrated to v%d, saved to %s", CURRENT_VERSION, state_path)
        return

    if args.clean:
        auto_clean(state)
        remove_unknown(state, config)
        save_state(state_path, state)
        log.info("Done. State saved to %s", state_path)
        return

    if not config.get("tasks"):
        log.error("No tasks defined in config.")
        sys.exit(1)

    configure_vasco()
    analysis = args.analysis
    collector = RunCapture(
        limit=args.analysis_limit_items if analysis else 0,
        limit_feeds=args.analysis_limit_feeds if analysis else 0,
    )

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": USER_AGENT},
    ) as session:
        ctx = RunContext(
            session=session,
            config=config,
            llm=_build_llm_handles(config),
            collector=collector,
            analysis=analysis,
        )

        if args.regenerate_state:
            await regenerate_feeds_state(config, state, ctx)
            save_state(state_path, state)
            log.info("Done. State regenerated and saved to %s", state_path)
            return

        coros = _collect_due_tasks(ctx, state, args.task, datetime.now(UTC))
        results = await asyncio.gather(*coros, return_exceptions=True)

        evals_dir = state_path.parent / "evals"
        written = collector.write_jsonl(evals_dir, run_local.strftime("%Y-%m-%dT%H-%M-%S"))
        if written:
            log.info("Wrote eval traces: %d file(s) under %s", len(written), evals_dir)

        if analysis:
            if args.human:
                collector.display()
            else:
                print(collector.to_json())
            return

        merge_task_results(state, results)
        save_state(state_path, state)
        log.info("Done. State saved to %s", state_path)


# --- One-shot CLI modes ---


def _mode_validate(args: argparse.Namespace) -> None:
    config_path, _ = resolve_paths(args)
    _load_valid_config(config_path)
    log.info("Config is valid: %s", config_path)


def _mode_stats(args: argparse.Namespace) -> None:
    config_path, state_path = resolve_paths(args)
    print_stats(load_config(config_path), load_state(state_path))


def _mode_summarize(args: argparse.Namespace) -> None:
    config_path, _ = resolve_paths(args)
    config = load_config(config_path)
    handle = build_model_handle(
        resolve_model_specs((config.get("summarize") or {}).get("model")),
        (config.get("llm") or {}).get("api_key") or None,
    )
    if handle is None:
        log.error("--summarize requires summarize.model with a provider configured")
        sys.exit(1)
    language = (config.get("curate") or {}).get("language") or "EN-US"
    asyncio.run(
        run_summarize(args.summarize, adapter=handle.adapter, model=handle.model, language=language)
    )


def main():
    parser = argparse.ArgumentParser(
        description="Feed aggregator and notifier: RSS feeds, real-estate listings, and LLM tasks posted to Discord"
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
        help="Remove state entries older than 30 days or missing first_seen, then exit",
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
        "--stats",
        action="store_true",
        help="Print a rich-formatted summary of state.json (last_run, next_run, item counts) and exit",
    )
    mode.add_argument(
        "--summarize",
        metavar="URL",
        help="Fetch transcript from a YouTube video and print a summary to stdout",
    )
    mode.add_argument(
        "--get-content",
        metavar="URL",
        dest="get_content",
        help="Fetch article text or YouTube transcript and print to stdout (no LLM)",
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

    logsetup.setup(verbose=args.verbose)

    if args.validate:
        _mode_validate(args)
    elif args.stats:
        _mode_stats(args)
    elif args.summarize:
        _mode_summarize(args)
    elif args.get_content:
        asyncio.run(run_get_content(args.get_content))
    else:
        asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
