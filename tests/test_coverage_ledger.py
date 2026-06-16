"""Unit tests for the coverage-ledger merge (tasks/feed_state.apply_coverage)."""

from datetime import UTC, datetime, timedelta

from pipeline import CoverageUpdate
from tasks.feed_state import LEDGER_ACTIVE_DAYS, apply_coverage

NOW = "2026-06-16T12:00:00+00:00"


def _cu(continues, label, state, citations=()):
    return CoverageUpdate(
        continues=continues, label=label, state=state, citations=list(citations), section=None
    )


def test_new_topic_seeded_with_frequency_one():
    ledger = apply_coverage([], [_cu(None, "US–Iran war", "Ceasefire signed.")], NOW)
    assert len(ledger) == 1
    e = ledger[0]
    assert e["id"] == "us-iran-war"  # slug of the label
    assert e["frequency"] == 1
    assert e["first_seen"] == NOW and e["last_seen"] == NOW
    assert e["state"] == "Ceasefire signed."


def test_continuation_bumps_frequency_preserves_first_seen():
    prev = [
        {
            "id": "us-iran-war",
            "label": "US–Iran war",
            "state": "old state",
            "first_seen": "2026-06-10T00:00:00+00:00",
            "last_seen": "2026-06-15T00:00:00+00:00",
            "frequency": 3,
        }
    ]
    ledger = apply_coverage(prev, [_cu("us-iran-war", "US–Iran war", "Strait reopened.")], NOW)
    e = ledger[0]
    assert e["frequency"] == 4
    assert e["state"] == "Strait reopened."  # state refreshed
    assert e["last_seen"] == NOW
    assert e["first_seen"] == "2026-06-10T00:00:00+00:00"  # preserved


def test_new_topic_with_colliding_slug_merges_not_duplicates():
    prev = [
        {
            "id": "us-iran-war",
            "label": "US–Iran war",
            "state": "old",
            "first_seen": NOW,
            "last_seen": "2026-06-15T00:00:00+00:00",
            "frequency": 1,
        }
    ]
    # continues=None, but the label slugifies to the existing id → merge into it.
    ledger = apply_coverage(prev, [_cu(None, "US Iran war", "update")], NOW)
    assert len(ledger) == 1
    assert ledger[0]["frequency"] == 2


def test_dormant_topics_evicted_by_age():
    old_ts = (datetime.now(UTC) - timedelta(days=LEDGER_ACTIVE_DAYS + 2)).isoformat()
    prev = [
        {
            "id": "stale",
            "label": "Stale",
            "state": "x",
            "first_seen": old_ts,
            "last_seen": old_ts,
            "frequency": 1,
        },
        {
            "id": "fresh",
            "label": "Fresh",
            "state": "y",
            "first_seen": old_ts,
            "last_seen": NOW,
            "frequency": 1,
        },
    ]
    ledger = apply_coverage(prev, [], NOW)  # no updates this run
    assert {e["id"] for e in ledger} == {"fresh"}
