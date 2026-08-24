# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Research task: agentic search/read loop over vascod, posted as plain text."""

import logging

from pipeline import Item
from pull.research import run_research_task
from tasks.context import RunContext
from tasks.delivery import deliver_text
from util import utc_now_iso

log = logging.getLogger(__name__)


async def process_research_task(task_cfg: dict, state: dict, ctx: RunContext) -> dict:
    """Run the agentic research loop, post the answer as plain text. Returns {name: state} or {}."""
    name = task_cfg["name"]
    with ctx.capture_task(name, "research"):
        handle = ctx.llm.research
        if handle is None:
            log.error("[%s] Skipping — research.model is not configured", name)
            return {}

        trace: dict | None = {} if ctx.collector else None
        text = await run_research_task(
            task_cfg,
            ctx.research_instructions,
            handle.model,
            adapter=handle.adapter,
            reasoning=handle.reasoning_for(ctx.analysis),
            trace=trace,
        )
        if ctx.collector and trace is not None:
            ctx.collector.record_research(
                model=trace.get("model"),
                instructions=trace.get("instructions"),
                prompt=trace.get("prompt", ""),
                answer=text,
                steps=trace.get("steps"),
                sources=trace.get("sources"),
                model_used=trace.get("model_used"),
            )

        if not text:
            if ctx.analysis:
                ctx.record_push(0)
            return {}
        items = [Item(id=f"{name}:research_result", title=name, source=name, body=text)]

        if ctx.analysis:
            ctx.record_push(len(items))
            return {}

        if not await deliver_text(ctx, task_cfg, items, name):
            return {}
        ctx.record_push(len(items))
        return {name: {"last_run": utc_now_iso()}}
