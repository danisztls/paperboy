"""State merging for RSS/digest tasks (per-feed items + the coverage ledger)."""

import logging
import re
from datetime import UTC, datetime, timedelta

from pipeline import CoverageUpdate, Item
from util import utc_now_iso

log = logging.getLogger(__name__)

LEDGER_ACTIVE_DAYS = 21  # evict topics not covered within this window
LEDGER_MAX_TOPICS = 60  # hard cap on ledger size (most-recently-seen kept)


def _slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s[:48] or "topic"


def apply_coverage(
    prev_ledger: list[dict], coverage: list[CoverageUpdate] | None, now_iso: str
) -> list[dict]:
    """Upsert this run's coverage updates into the topic ledger.

    Each update either continues an existing topic (bumping `frequency`, refreshing
    `state`/`last_seen`) or creates one (id = slug of the label; a colliding slug merges
    into the existing topic). Topics not seen within LEDGER_ACTIVE_DAYS are evicted and
    the ledger is capped at LEDGER_MAX_TOPICS (most-recently-seen kept). Code owns
    frequency/timestamps so the trajectory bar reads a real count, not an LLM guess.
    """
    by_id = {e["id"]: dict(e) for e in prev_ledger}
    for u in coverage or []:
        tid = u.continues if (u.continues and u.continues in by_id) else _slugify(u.label)
        entry = by_id.get(tid)
        if entry is None:
            entry = {"id": tid, "label": u.label, "first_seen": now_iso, "frequency": 0}
            by_id[tid] = entry
        entry["label"] = u.label or entry.get("label", tid)
        entry["state"] = u.state
        entry["last_seen"] = now_iso
        entry["frequency"] = int(entry.get("frequency", 0)) + 1

    cutoff = datetime.now(UTC) - timedelta(days=LEDGER_ACTIVE_DAYS)

    def _seen(e: dict) -> datetime:
        try:
            return datetime.fromisoformat(e.get("last_seen", ""))
        except ValueError:
            return datetime.now(UTC)

    ledger = [e for e in by_id.values() if _seen(e) >= cutoff]
    ledger.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
    return ledger[:LEDGER_MAX_TOPICS]


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
    prev_ledger: list[dict],
    coverage: list[CoverageUpdate] | None,
    task_name: str,
) -> dict:
    """Merge per-feed state, update the coverage ledger; returns the task_state dict."""
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
        ledger = apply_coverage(prev_ledger, coverage, now_iso)
        new_task_state["coverage"] = {"ledger": ledger}
        if coverage:
            log.info(
                "[%s] Coverage ledger: %d topics (%d touched this run)",
                task_name,
                len(ledger),
                len(coverage),
            )
    return new_task_state
