"""State file schema migrations."""

CURRENT_VERSION = 2


def needs_migration(state: dict) -> bool:
    return state.get("_version", 0) < CURRENT_VERSION


def _to_v2(state: dict) -> dict:
    """Nest all task keys under 'tasks:' (v0/v1 → v2)."""
    _meta = {"_version", "_last_run", "_last_clean"}
    tasks = {k: v for k, v in state.items() if k not in _meta}
    new = {k: v for k, v in state.items() if k in _meta}
    new["tasks"] = tasks
    new["_version"] = CURRENT_VERSION
    return new


_STEPS = {0: _to_v2, 1: _to_v2}


def migrate(state: dict) -> dict:
    version = state.get("_version", 0)
    while version < CURRENT_VERSION:
        fn = _STEPS.get(version)
        if fn is None:
            raise ValueError(f"No migration defined from version {version}")
        state = fn(state)
        version = state.get("_version", 0)
    return state
