# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Logging configuration: journald / rich-tty / plain handlers + per-run file log."""

import logging
import os
import pathlib
import sys
from datetime import datetime

# Our own package loggers. Setting a parent (e.g. "pull") to DEBUG propagates to
# its children ("pull.feed", …); third-party libraries are left alone.
APP_LOGGERS = (
    "__main__",
    "main",
    "tasks",
    "stats",
    "pull",
    "push",
    "process",
    "state",
    "config",
    "providers",
    "evals",
)

# Third-party SDK chatter we never want in our logs:
# - httpx/httpcore: per-request transport lines
# - openai: "Retrying request to /chat/completions in N seconds" (the SDK's own
#   transient-failure retries; the real outcome is logged by timed_call + the
#   FallbackAdapter, so these are redundant noise)
# - google_genai: "AFC is enabled with max remote calls: 10." (Automatic Function
#   Calling notice emitted on every Gemini call)
_NOISY_LOGGERS = ("httpx", "httpcore", "openai", "google_genai")

_FILE_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

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


def _under_systemd() -> bool:
    # JOURNAL_STREAM is set when stdout/stderr is captured by journald
    return "JOURNAL_STREAM" in os.environ


def setup(verbose: bool = False) -> None:
    """Configure the root logger for the current environment (journald / tty / pipe)."""
    if _under_systemd():
        handler = logging.StreamHandler()
        handler.setFormatter(_JournaldFormatter("[%(name)s] %(message)s"))
        # Journal shows the INFO heartbeat only; full DEBUG detail goes to the
        # per-run file log (added later via add_file_handler). Root stays at INFO
        # so noisy third-party loggers are unaffected; our loggers go to DEBUG so
        # their records reach the file handler while journald filters them out.
        handler.setLevel(logging.INFO)
        logging.basicConfig(handlers=[handler], level=logging.INFO)
        for name in APP_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
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

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    if verbose:
        for name in APP_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)


def add_file_handler(logs_dir: pathlib.Path, ts: datetime) -> None:
    """Attach the per-run DEBUG file log under `logs_dir`."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y-%m-%dT%H-%M-%S")
    handler = logging.FileHandler(logs_dir / f"{stamp}.log", encoding="utf-8")
    handler.setFormatter(_FILE_FORMAT)
    logging.getLogger().addHandler(handler)
