"""Orchestrator-level invariant: empty task results must not touch state."""

from main import merge_task_results


def test_empty_result_leaves_state_unchanged():
    state = {
        "tasks": {
            "task-a": {
                "feeds": {"u1": {"items": [{"url": "x"}], "last_run": "2026-01-01T00:00:00+00:00"}}
            }
        }
    }
    snapshot = {"tasks": {"task-a": dict(state["tasks"]["task-a"])}}
    merge_task_results(state, [{}])
    assert state == snapshot


def test_exception_result_leaves_state_unchanged():
    state = {"tasks": {"task-a": {"last_run": "2026-01-01T00:00:00+00:00"}}}
    merge_task_results(state, [RuntimeError("boom")])
    assert state["tasks"]["task-a"]["last_run"] == "2026-01-01T00:00:00+00:00"


def test_nonempty_result_merges_per_task():
    state = {"tasks": {"task-a": {"old_key": "kept"}}}
    merge_task_results(state, [{"task-a": {"last_run": "2026-05-14T00:00:00+00:00"}}])
    assert state["tasks"]["task-a"] == {
        "old_key": "kept",
        "last_run": "2026-05-14T00:00:00+00:00",
    }


def test_new_task_added():
    state: dict = {"tasks": {}}
    merge_task_results(state, [{"new-task": {"last_run": "2026-05-14T00:00:00+00:00"}}])
    assert state["tasks"]["new-task"] == {"last_run": "2026-05-14T00:00:00+00:00"}


def test_mixed_empty_and_nonempty():
    """One task succeeds, another fails — only the success updates."""
    state = {"tasks": {"good": {"last_run": "old"}, "bad": {"last_run": "stale"}}}
    merge_task_results(state, [{"good": {"last_run": "new"}}, {}, RuntimeError("x")])
    assert state["tasks"]["good"]["last_run"] == "new"
    assert state["tasks"]["bad"]["last_run"] == "stale"
