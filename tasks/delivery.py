"""Shared push step for tasks that post plain text (research, weather, finance)."""

import logging

from config import get_file_path
from pipeline import Item, PushContext
from push.discord import DiscordTextTarget
from push.file import FileItemTarget
from tasks.context import RunContext

log = logging.getLogger(__name__)


async def deliver_text(ctx: RunContext, task_cfg: dict, items: list[Item], name: str) -> bool:
    """Post item bodies as Discord text, then the optional file target.

    Returns False when the Discord post failed (caller must not save state so
    the task retries next sweep).
    """
    push_ctx = PushContext(items=items)
    try:
        await DiscordTextTarget().push(push_ctx, task_cfg, ctx.session)
    except Exception:
        log.error("[%s] Skipping task due to post failure", name)
        return False
    log.info("[%s] Posted to Discord", name)
    if get_file_path(task_cfg):
        await FileItemTarget().push(push_ctx, task_cfg, ctx.session)
    return True
