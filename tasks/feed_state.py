"""State merging for RSS/digest tasks (per-feed items + the coverage ledger)."""

import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from pipeline import CoverageUpdate, Item
from util import utc_now_iso

log = logging.getLogger(__name__)

LEDGER_ACTIVE_DAYS = 21  # topics dormant longer than this leave the active ledger
LEDGER_MAX_TOPICS = 60  # hard cap on the active ledger (most-recently-seen kept)
ROLLUP_MAX_MONTHS = 6  # months of aged backdrop retained
ROLLUP_MAX_PER_PERIOD = 15  # top topics kept per month bucket (by frequency)


def _slugify(label: str) -> str:
    # Transliterate accents to ASCII (ç→c, ã→a) before dropping non-alphanumerics,
    # so "Eleição" slugifies to "eleicao", not "elei-o".
    nfkd = unicodedata.normalize("NFKD", label or "")
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()
    s = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")
    return s[:48] or "topic"


def apply_coverage(
    prev_coverage: dict, coverage: list[CoverageUpdate] | None, now_iso: str
) -> dict:
    """Update the coverage state: an active topic ledger + aged month rollups.

    This run's updates upsert into the active ledger — each continues an existing topic
    (bumping `frequency`, refreshing `state`/`last_seen`) or creates one (id = slug of the
    label; a colliding slug merges in). Code owns frequency/timestamps so the trajectory
    bar reads a real count. Topics dormant past LEDGER_ACTIVE_DAYS leave the active ledger
    and fold into per-month `rollups` buckets (the long-horizon, cutoff-gap backdrop):
    each bucket keeps the top ROLLUP_MAX_PER_PERIOD topics by frequency, and the most
    recent ROLLUP_MAX_MONTHS buckets are retained. Returns ``{"ledger": [...], "rollups": [...]}``.
    """
    by_id = {e["id"]: dict(e) for e in prev_coverage.get("ledger", [])}
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

    active, dormant = [], []
    for e in by_id.values():
        (active if _seen(e) >= cutoff else dormant).append(e)

    # Fold dormant topics into per-month rollup buckets.
    rollups = {
        r["period"]: {"period": r["period"], "topics": list(r.get("topics", []))}
        for r in prev_coverage.get("rollups", [])
    }
    for e in dormant:
        period = str(e.get("last_seen", ""))[:7] or "unknown"
        bucket = rollups.setdefault(period, {"period": period, "topics": []})
        topic = {
            "id": e["id"],
            "label": e.get("label", e["id"]),
            "state": e.get("state", ""),
            "frequency": int(e.get("frequency", 1)),
        }
        existing = next((t for t in bucket["topics"] if t.get("id") == e["id"]), None)
        if existing:
            existing.update(topic)
        else:
            bucket["topics"].append(topic)
    for b in rollups.values():
        b["topics"].sort(key=lambda t: t.get("frequency", 1), reverse=True)
        b["topics"] = b["topics"][:ROLLUP_MAX_PER_PERIOD]
    rollup_list = sorted(rollups.values(), key=lambda r: r["period"], reverse=True)[
        :ROLLUP_MAX_MONTHS
    ]

    active.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
    return {"ledger": active[:LEDGER_MAX_TOPICS], "rollups": rollup_list}


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
    prev_coverage: dict,
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
        cov = apply_coverage(prev_coverage, coverage, now_iso)
        new_task_state["coverage"] = cov
        if coverage:
            log.info(
                "[%s] Coverage: %d active topics, %d rollup months (%d touched this run)",
                task_name,
                len(cov["ledger"]),
                len(cov["rollups"]),
                len(coverage),
            )
    return new_task_state
