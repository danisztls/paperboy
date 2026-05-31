from state.migrate import CURRENT_VERSION, migrate, needs_migration


def test_v4_moves_flat_items_to_legacy_bucket():
    state = {
        "_version": 3,
        "tasks": {
            "imoveis": {
                "last_run": "2026-05-19T10:00:00+00:00",
                "items": [
                    {"url": "https://x/1", "title": "A", "first_seen": "2026-05-17T12:00:00+00:00"},
                    {"url": "https://x/2", "title": "B", "first_seen": "2026-05-18T12:00:00+00:00"},
                ],
            }
        },
    }
    migrated = migrate(state)

    assert migrated["_version"] == CURRENT_VERSION
    imoveis = migrated["tasks"]["imoveis"]
    assert "items" not in imoveis
    assert imoveis["last_run"] == "2026-05-19T10:00:00+00:00"
    # v4 created the bucket; v6 renamed it scrapers → realestate.
    assert "scrapers" not in imoveis
    assert imoveis["realestate"]["__legacy__"]["items"] == [
        {"url": "https://x/1", "title": "A", "first_seen": "2026-05-17T12:00:00+00:00"},
        {"url": "https://x/2", "title": "B", "first_seen": "2026-05-18T12:00:00+00:00"},
    ]


def test_v4_skips_tasks_without_flat_items():
    state = {
        "_version": 3,
        "tasks": {
            "world-news": {"last_run": "2026-05-19T10:00:00+00:00"},
            "feeds-task": {
                "feeds": {"https://f/x": {"items": [], "last_run": "2026-05-19T10:00:00+00:00"}}
            },
            "weather": {"last_run": "2026-05-19T10:00:00+00:00", "climate": {"month": "2026-05"}},
        },
    }
    migrated = migrate(state)

    assert migrated["_version"] == CURRENT_VERSION
    for task in migrated["tasks"].values():
        assert "scrapers" not in task
        assert "realestate" not in task


def _v4_state_with_adapter_buckets():
    return {
        "_version": 4,
        "tasks": {
            "imoveis": {
                "scrapers": {
                    "vivareal": {
                        "items": [
                            {
                                "url": "https://v/1",
                                "title": "X",
                                "first_seen": "2026-05-19T10:00:00+00:00",
                            }
                        ],
                        "last_run": "2026-05-19T10:00:00+00:00",
                    },
                    "__legacy__": {
                        "items": [
                            {
                                "url": "https://leg/1",
                                "title": "L",
                                "first_seen": "2026-05-15T10:00:00+00:00",
                            }
                        ]
                    },
                }
            }
        },
    }


def test_v5_rekeys_adapter_buckets_to_url_with_config():
    state = _v4_state_with_adapter_buckets()
    config = {
        "tasks": [
            {
                "name": "imoveis",
                "pull": [
                    {"realestate": {"adapter": "vivareal", "url": "https://vivareal.com.br/search"}}
                ],
            }
        ]
    }
    migrated = migrate(state, config)

    # v5 rekeys adapter→url; v6 renames the bucket scrapers → realestate.
    buckets = migrated["tasks"]["imoveis"]["realestate"]
    assert migrated["_version"] == CURRENT_VERSION
    assert "scrapers" not in migrated["tasks"]["imoveis"]
    assert "vivareal" not in buckets
    assert buckets["https://vivareal.com.br/search"]["items"][0]["url"] == "https://v/1"
    # __legacy__ is untouched by a clean rekey.
    assert [it["url"] for it in buckets["__legacy__"]["items"]] == ["https://leg/1"]


def test_v5_folds_unmapped_buckets_into_legacy_without_config():
    state = _v4_state_with_adapter_buckets()
    migrated = migrate(state)  # no config → can't rekey, must preserve dedup urls

    buckets = migrated["tasks"]["imoveis"]["realestate"]
    assert migrated["_version"] == CURRENT_VERSION
    assert "vivareal" not in buckets
    assert set(buckets) == {"__legacy__"}
    assert {it["url"] for it in buckets["__legacy__"]["items"]} == {
        "https://leg/1",
        "https://v/1",
    }


def test_v6_renames_scrapers_bucket_to_realestate():
    state = {
        "_version": 5,
        "tasks": {
            "imoveis": {
                "scrapers": {
                    "https://vivareal.com.br/search": {
                        "items": [{"url": "https://v/1"}],
                        "last_run": "2026-05-19T10:00:00+00:00",
                    },
                    "__legacy__": {"items": [{"url": "https://leg/1"}]},
                }
            },
            "world-news": {"feeds": {"https://f/x": {"items": []}}},
        },
    }
    migrated = migrate(state)

    imoveis = migrated["tasks"]["imoveis"]
    assert migrated["_version"] == CURRENT_VERSION
    assert "scrapers" not in imoveis
    assert set(imoveis["realestate"]) == {"https://vivareal.com.br/search", "__legacy__"}
    # Non-real-estate tasks are untouched.
    assert "realestate" not in migrated["tasks"]["world-news"]


def test_needs_migration_at_current_version():
    assert needs_migration({"_version": CURRENT_VERSION}) is False
    assert needs_migration({"_version": 0}) is True
