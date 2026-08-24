# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Layered config resolution: merge global → task → feed config blocks.

Several config blocks (`ignore`, `skip`, `description`, `title`, `discord`) can be set at more
than one scope — globally, per task, and per feed — with the more specific scope
overriding the broader one per leaf key. `layer_dict` is the single primitive for
that resolution: pass the already-extracted blocks in low→high precedence order and
read the leaf key off the merged result.

    color = parse_color(layer_dict(
        config.get("discord"), get_discord_cfg(task_cfg), feed.get("discord")
    ).get("color"))

Each scope's block is extracted by the caller, because the accessors differ — e.g.
the task-level Discord block lives under `push[].discord` (via `get_discord_cfg`),
not `task["discord"]`. Merging the extracted blocks keeps this one helper agnostic
to where each scope stores its block.
"""

from __future__ import annotations


def layer_dict(*blocks: dict | None) -> dict:
    """Shallow-merge dict blocks; later blocks override earlier ones per key.

    Blocks are passed low→high precedence (global, task, feed). Non-dict blocks
    (``None`` / absent) are skipped, so callers can pass ``cfg.get("ignore")``
    directly without guarding.
    """
    merged: dict = {}
    for block in blocks:
        if isinstance(block, dict):
            merged.update(block)
    return merged


def resolve_scoped(
    key: str, global_cfg: dict, task_cfg: dict, feed_cfg: dict, *, youtube: bool
) -> dict:
    """layer_dict a scoped block (global→task→feed) by `key`. When `youtube` is True (feed is a
    YouTube feed), interleave the global/task `youtube.<key>` contributions at the matching scope,
    so a global `youtube.ignore.description` is overridable per task/feed."""
    blocks: list = [global_cfg.get(key)]
    if youtube:
        blocks.append((global_cfg.get("youtube") or {}).get(key))
    blocks.append(task_cfg.get(key))
    if youtube:
        blocks.append((task_cfg.get("youtube") or {}).get(key))
    blocks.append(feed_cfg.get(key))
    return layer_dict(*blocks)
