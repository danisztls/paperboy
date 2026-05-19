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
    assert imoveis["scrapers"]["__legacy__"]["items"] == [
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
    assert "scrapers" not in migrated["tasks"]["world-news"]
    assert "scrapers" not in migrated["tasks"]["feeds-task"]
    assert "scrapers" not in migrated["tasks"]["weather"]


def test_v4_preserves_existing_scrapers_dict():
    state = {
        "_version": 3,
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
                    }
                },
                "items": [
                    {
                        "url": "https://leg/1",
                        "title": "L",
                        "first_seen": "2026-05-15T10:00:00+00:00",
                    }
                ],
            }
        },
    }
    migrated = migrate(state)

    scrapers = migrated["tasks"]["imoveis"]["scrapers"]
    assert "vivareal" in scrapers
    assert len(scrapers["vivareal"]["items"]) == 1
    assert "__legacy__" in scrapers
    assert len(scrapers["__legacy__"]["items"]) == 1


def test_needs_migration_at_current_version():
    assert needs_migration({"_version": CURRENT_VERSION}) is False
    assert needs_migration({"_version": 0}) is True
