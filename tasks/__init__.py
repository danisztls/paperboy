# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Task orchestration: one `process_*_task(task_cfg, state, ctx)` per task kind.

Each processor pulls from its source(s), pushes to the configured targets, and
returns a `{task_name: task_state}` slice to merge into state — or `{}` when
nothing should be persisted (failed pull/post, dry-run). Shared dependencies
ride on `RunContext`; due checks live in `tasks.due`.
"""

from tasks.context import LLMHandles, RunContext
from tasks.due import DEFAULT_PERIOD, due_feeds, is_due, task_is_due
from tasks.feeds import process_feed_task, regenerate_feeds_state
from tasks.finance import process_finance_task
from tasks.realestate import process_realestate_task
from tasks.research import process_research_task
from tasks.weather import process_weather_task

PROCESSORS = {
    "research": process_research_task,
    "realestate": process_realestate_task,
    "weather": process_weather_task,
    "finance": process_finance_task,
    # any other kind (feeds, digest) is handled by process_feed_task
}


def processor_for(kind: str):
    return PROCESSORS.get(kind, process_feed_task)


__all__ = [
    "DEFAULT_PERIOD",
    "LLMHandles",
    "RunContext",
    "due_feeds",
    "is_due",
    "process_feed_task",
    "process_finance_task",
    "process_realestate_task",
    "process_research_task",
    "process_weather_task",
    "processor_for",
    "regenerate_feeds_state",
    "task_is_due",
]
