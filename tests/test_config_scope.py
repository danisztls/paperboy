from config.scope import layer_dict


def test_layer_dict_empty():
    assert layer_dict() == {}
    assert layer_dict({}, {}) == {}
    assert layer_dict(None, None) == {}


def test_layer_dict_skips_non_dict_blocks():
    # callers pass cfg.get("ignore") directly; absent → None → skipped
    assert layer_dict(None, {"a": 1}, None) == {"a": 1}


def test_layer_dict_single_block():
    block = {"description": {"remove": "x"}}
    assert layer_dict(block) == block
    assert layer_dict(None, block) == block


def test_layer_dict_disjoint_keys_union():
    task_f = {"image": True}
    feed_f = {"description": False}
    assert layer_dict(task_f, feed_f) == {"image": True, "description": False}


def test_layer_dict_later_block_overrides_per_key():
    """The Cappy Army case: feed-level `description: false` overrides task-level `true`."""
    task_f = {"description": True}
    feed_f = {"description": False}
    assert layer_dict(task_f, feed_f) == {"description": False}


def test_layer_dict_global_task_feed_precedence():
    glob = {"shorts": True, "livestreams": True}
    task = {"livestreams": False}
    feed = {"shorts": False}
    # feed > task > global, per leaf key
    assert layer_dict(glob, task, feed) == {"shorts": False, "livestreams": False}


def test_layer_dict_does_not_mutate_inputs():
    glob = {"a": 1}
    task = {"b": 2}
    layer_dict(glob, task)
    assert glob == {"a": 1}
    assert task == {"b": 2}


def test_resolve_scoped_youtube_gate_applies_only_to_youtube_feeds():
    from config.scope import resolve_scoped

    g = {"ignore": {"image": True}, "youtube": {"ignore": {"description": True}}}
    yt_fc = {"url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCx"}
    other_fc = {"url": "https://example.com/rss"}
    # YouTube feed: the global youtube.ignore.description merges in alongside the plain ignore.image
    assert resolve_scoped("ignore", g, {}, yt_fc, youtube=True) == {
        "image": True,
        "description": True,
    }
    # Non-YouTube feed: the youtube scope is not applied
    assert resolve_scoped("ignore", g, {}, other_fc, youtube=False) == {"image": True}


def test_resolve_scoped_feed_overrides_youtube_scope():
    """A per-channel `ignore.description: false` opts out of a global youtube lever."""
    from config.scope import resolve_scoped

    g = {"youtube": {"ignore": {"description": True}}}
    fc = {
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCx",
        "ignore": {"description": False},
    }
    assert resolve_scoped("ignore", g, {}, fc, youtube=True) == {"description": False}
