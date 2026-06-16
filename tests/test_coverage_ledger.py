"""Unit tests for the coverage merge (tasks/feed_state.apply_coverage): active ledger + rollups."""

from datetime import UTC, datetime, timedelta

from pipeline import CoverageUpdate
from tasks.feed_state import LEDGER_ACTIVE_DAYS, apply_coverage

NOW = "2026-06-16T12:00:00+00:00"


def _cu(continues, label, state, citations=()):
    return CoverageUpdate(
        continues=continues, label=label, state=state, citations=list(citations), section=None
    )


def _ledger(prev):
    return {"ledger": prev}


def test_new_topic_seeded_with_frequency_one():
    cov = apply_coverage({}, [_cu(None, "US–Iran war", "Ceasefire signed.")], NOW)
    assert len(cov["ledger"]) == 1
    e = cov["ledger"][0]
    assert e["id"] == "us-iran-war"  # slug of the label
    assert e["frequency"] == 1
    assert e["first_seen"] == NOW and e["last_seen"] == NOW
    assert e["state"] == "Ceasefire signed."
    assert cov["rollups"] == []


def test_slug_transliterates_accents():
    cov = apply_coverage({}, [_cu(None, "Eleição presidencial no Peru 2026", "x")], NOW)
    assert cov["ledger"][0]["id"] == "eleicao-presidencial-no-peru-2026"


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
    e = apply_coverage(_ledger(prev), [_cu("us-iran-war", "US–Iran war", "Strait reopened.")], NOW)[
        "ledger"
    ][0]
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
    ledger = apply_coverage(_ledger(prev), [_cu(None, "US Iran war", "update")], NOW)["ledger"]
    assert len(ledger) == 1
    assert ledger[0]["frequency"] == 2


def test_dormant_topic_evicted_and_folded_into_rollup():
    old_ts = (datetime.now(UTC) - timedelta(days=LEDGER_ACTIVE_DAYS + 2)).isoformat()
    prev = [
        {
            "id": "stale",
            "label": "Stale topic",
            "state": "x",
            "first_seen": old_ts,
            "last_seen": old_ts,
            "frequency": 5,
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
    cov = apply_coverage(_ledger(prev), [], NOW)  # no updates this run
    assert {e["id"] for e in cov["ledger"]} == {"fresh"}  # stale left the active ledger
    # ...and folded into a month rollup bucket keyed by its last_seen month, freq preserved.
    bucket = next(r for r in cov["rollups"] if r["period"] == old_ts[:7])
    rolled = next(t for t in bucket["topics"] if t["id"] == "stale")
    assert rolled["frequency"] == 5 and rolled["label"] == "Stale topic"


def test_rollups_carry_forward_across_runs():
    prev = {
        "ledger": [],
        "rollups": [
            {
                "period": "2026-04",
                "topics": [{"id": "old", "label": "Old arc", "state": "z", "frequency": 9}],
            }
        ],
    }
    cov = apply_coverage(prev, [_cu(None, "New thing", "fresh state")], NOW)
    assert any(r["period"] == "2026-04" for r in cov["rollups"])  # prior rollup preserved
    assert cov["ledger"][0]["id"] == "new-thing"  # new active topic added
