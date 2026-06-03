from config.scope import layer_dict


def test_layer_dict_empty():
    assert layer_dict() == {}
    assert layer_dict({}, {}) == {}
    assert layer_dict(None, None) == {}


def test_layer_dict_skips_non_dict_blocks():
    # callers pass cfg.get("filter") directly; absent → None → skipped
    assert layer_dict(None, {"a": 1}, None) == {"a": 1}


def test_layer_dict_single_block():
    block = {"title": [{"extract": "x"}]}
    assert layer_dict(block) == block
    assert layer_dict(None, block) == block


def test_layer_dict_disjoint_keys_union():
    task_f = {"title": [{"extract": "x"}]}
    feed_f = {"url": {"skip_containing": "/foo"}}
    assert layer_dict(task_f, feed_f) == {
        "title": [{"extract": "x"}],
        "url": {"skip_containing": "/foo"},
    }


def test_layer_dict_later_block_overrides_per_key():
    """The Cappy Army case: feed-level `clear: false` overrides task-level `clear: true`."""
    task_f = {"description": {"clear": True}}
    feed_f = {"description": {"clear": False}}
    assert layer_dict(task_f, feed_f) == {"description": {"clear": False}}


def test_layer_dict_global_task_feed_precedence():
    glob = {"ignore_shorts": True, "ignore_livestreams": True}
    task = {"ignore_livestreams": False}
    feed = {"ignore_shorts": False}
    # feed > task > global, per leaf key
    assert layer_dict(glob, task, feed) == {
        "ignore_shorts": False,
        "ignore_livestreams": False,
    }


def test_layer_dict_does_not_mutate_inputs():
    glob = {"a": 1}
    task = {"b": 2}
    layer_dict(glob, task)
    assert glob == {"a": 1}
    assert task == {"b": 2}
