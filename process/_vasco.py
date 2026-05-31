"""Bridge to vasco for URL content extraction and raw HTML fetching."""

from __future__ import annotations

import logging
from typing import Any

from vasco.cache import Cache
from vasco.config import Config, load_config
from vasco.fetch import fetch_one

log = logging.getLogger(__name__)

_cfg: Config | None = None
_cache: Cache | None = None


def configure() -> None:
    global _cfg, _cache
    _cfg = load_config()
    _cache = Cache()


def _envelope_ok(env: dict[str, Any]) -> bool:
    return "failure" not in env and bool(env.get("markdown"))


async def fetch_content(url: str, *, refresh: bool = False) -> tuple[str, str | None] | None:
    env = await fetch_one(url, mode="auto", refresh=refresh, cache=_cache, cfg=_cfg)
    if not _envelope_ok(env):
        log.warning("vasco fetch failed for %s: %s", url, env.get("failure", {}).get("message", ""))
        return None
    return env["markdown"], env.get("image")


async def fetch_content_with_title(
    url: str, *, refresh: bool = False
) -> tuple[str, str, str | None, bool] | None:
    env = await fetch_one(url, mode="auto", refresh=refresh, cache=_cache, cfg=_cfg)
    if not _envelope_ok(env):
        log.warning("vasco fetch failed for %s: %s", url, env.get("failure", {}).get("message", ""))
        return None
    is_youtube = env.get("mode_used") == "youtube"
    return env["markdown"], env.get("title") or "", env.get("image"), is_youtube


async def fetch_raw_html(url: str, *, mode: str = "auto") -> str | None:
    env = await fetch_one(url, mode=mode, raw=True, cache=_cache, cfg=_cfg)
    if not _envelope_ok(env):
        log.warning("vasco fetch failed for %s: %s", url, env.get("failure", {}).get("message", ""))
        return None
    return env["markdown"]


async def fetch_listings(url: str, *, refresh: bool = False) -> dict | None:
    """Fetch structured real-estate listings via vasco's realestate adapter.

    Returns the full envelope (listings in ``env["quality"]["listings"]``,
    provider display name in ``env["site_name"]``) or ``None`` on failure / when
    the URL wasn't routed to the realestate adapter.
    """
    env = await fetch_one(url, mode="auto", refresh=refresh, cache=_cache, cfg=_cfg)
    if "failure" in env:
        log.warning("vasco fetch failed for %s: %s", url, env.get("failure", {}).get("message", ""))
        return None
    if env.get("mode_used") != "realestate":
        # Adapter routing only runs on a cache miss, so a browser-mode entry
        # cached before the realestate adapter existed can shadow it. Force one
        # refresh to re-route through the adapter, then give up.
        if not refresh:
            return await fetch_listings(url, refresh=True)
        log.warning(
            "vasco did not route %s to realestate adapter (mode=%s)", url, env.get("mode_used")
        )
        return None
    return env
