# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Finance task: yfinance quotes posted as a report or batched monitor alerts."""

import logging

from config import get_finance_cfg
from pull.finance import FinanceSource
from tasks.context import RunContext
from tasks.delivery import deliver_text
from util import utc_now_iso

log = logging.getLogger(__name__)


async def process_finance_task(task_cfg: dict, state: dict, ctx: RunContext) -> dict:
    """Fetch yfinance quotes, post report or batched monitor alerts. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    with ctx.capture_task(name, "finance"):
        finance_cfg = dict(get_finance_cfg(task_cfg))
        finance_cfg["_task_name"] = name

        task_state = state.get("tasks", {}).get(name, {})
        is_monitor = "monitor" in finance_cfg
        if is_monitor:
            finance_cfg["_state_tickers"] = task_state.get("tickers", {})
            finance_cfg["_last_run"] = task_state.get("last_run")

        result = await FinanceSource().pull(finance_cfg, set(), ctx.session)
        if result is None:
            return {}

        out_state: dict = {"last_run": utc_now_iso()}
        if is_monitor:
            out_state["tickers"] = finance_cfg.get("_new_state_tickers", {})

        if not result.new_items:
            # Monitor with zero alerts: still persist state so next tick has baselines.
            ctx.record_push(0)
            return {name: out_state} if is_monitor else {}

        if ctx.analysis:
            ctx.record_push(len(result.new_items))
            return {}

        if not await deliver_text(ctx, task_cfg, result.new_items, name):
            return {}

        ctx.record_push(len(result.new_items))
        return {name: out_state}
