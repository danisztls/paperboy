"""State merging for RSS/digest tasks (per-feed items + memory log)."""

import logging

from pipeline import Item, MemoryParagraph
from util import utc_now_iso

log = logging.getLogger(__name__)

MEMORY_MAX_ENTRIES = 20  # memory log cap; oldest evicted
MEMORY_CONTEXT_ENTRIES = 5  # entries sent to the LLM as context each run


def merge_feed_state(
    prev_items: list[dict],
    current_items: list[dict],
    annotated_by_link: dict[str, Item],
    *,
    has_curate: bool,
    failed_ids: set[str],
    now_iso: str,
) -> list[dict]:
    """Merge prior feed state with new pull results.

    Unseen current items become state dicts with optional summary and (under
    a curate filter) filter_pass/filter_reason annotations. Items that failed
    to post are dropped. first_seen is stamped on any item that lacks it.
    """
    prev_by_url = {item["url"]: item for item in prev_items}
    new_items: list[dict] = []
    for ci in current_items:
        if ci["url"] in prev_by_url:
            continue
        state_item = dict(ci)
        it = annotated_by_link.get(ci["url"])
        if it is not None and it.summary:
            state_item["summary"] = it.summary
        if has_curate:
            if it is not None and it.filter_pass is not None:
                state_item["filter_pass"] = it.filter_pass
                state_item["filter_reason"] = it.filter_reason or ""
            else:
                state_item["filter_pass"] = True
        new_items.append(state_item)

    final = list(prev_items) + new_items
    if failed_ids:
        final = [item for item in final if item["url"] not in failed_ids]
    for item in final:
        if "first_seen" not in item:
            item["first_seen"] = now_iso
    return final


def build_feed_task_state(
    *,
    feed_cfgs: list[dict],
    fetch_map: dict[str, object],
    feeds_state: dict,
    all_annotated: list[Item],
    has_curate: bool,
    failed_ids: set[str],
    raw_history: dict,
    memory_paragraphs: list[MemoryParagraph] | None,
    task_name: str,
) -> dict:
    """Merge per-feed state, update memory log; returns the task_state dict."""
    now_iso = utc_now_iso()
    new_feeds_state = dict(feeds_state)
    annotated_by_link = {it.id: it for it in all_annotated}

    for fc in feed_cfgs:
        url = fc["url"]
        pull_result = fetch_map.get(url)
        if pull_result is None:
            continue  # failed fetch — leave existing state untouched
        final_items = merge_feed_state(
            prev_items=feeds_state.get(url, {}).get("items", []),
            current_items=pull_result.current_items,
            annotated_by_link=annotated_by_link,
            has_curate=has_curate,
            failed_ids=failed_ids,
            now_iso=now_iso,
        )
        feed_dict: dict = {"items": final_items, "last_run": now_iso}
        if pull_result.name:
            feed_dict["name"] = pull_result.name
        new_feeds_state[url] = feed_dict

    new_task_state: dict = {"feeds": new_feeds_state}
    if has_curate:
        history = dict(raw_history)
        if memory_paragraphs is not None:
            joined = "\n\n".join(p.text for p in memory_paragraphs)
            history[now_iso] = " ".join(
                line.strip() for line in joined.splitlines() if line.strip()
            )
            if len(history) > MEMORY_MAX_ENTRIES:
                for old_key in sorted(history)[: len(history) - MEMORY_MAX_ENTRIES]:
                    del history[old_key]
            log.info("[%s] Memory updated (%d chars)", task_name, len(joined))
        new_task_state["memory"] = history
    return new_task_state
