from tasks import _merge_filter


def test_merge_filter_both_empty():
    assert _merge_filter({}, {}) == {}


def test_merge_filter_only_task():
    task_f = {"title": [{"extract": "x"}]}
    assert _merge_filter(task_f, {}) == task_f


def test_merge_filter_only_feed():
    feed_f = {"url": {"skip_containing": "/foo"}}
    assert _merge_filter({}, feed_f) == feed_f


def test_merge_filter_disjoint_keys():
    task_f = {"title": [{"extract": "x"}]}
    feed_f = {"url": {"skip_containing": "/foo"}}
    merged = _merge_filter(task_f, feed_f)
    assert merged == {"title": [{"extract": "x"}], "url": {"skip_containing": "/foo"}}


def test_merge_filter_shared_key_feed_replaces_task():
    task_f = {"description": [{"clear": True}]}
    feed_f = {"description": [{"remove_phrases_with_urls": True}]}
    assert _merge_filter(task_f, feed_f) == {"description": [{"remove_phrases_with_urls": True}]}


def test_merge_filter_feed_can_disable_task_clear():
    """The Cappy Army case: feed-level `clear: false` overrides task-level `clear: true`."""
    task_f = {"description": {"clear": True}}
    feed_f = {"description": {"clear": False}}
    assert _merge_filter(task_f, feed_f) == {"description": {"clear": False}}


def test_merge_filter_shared_key_single_dict_feed_replaces():
    task_f = {"title": {"extract": "x"}}
    feed_f = {"title": {"replace": "y", "with": "z"}}
    assert _merge_filter(task_f, feed_f) == {"title": {"replace": "y", "with": "z"}}
