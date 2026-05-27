"""Bridge to vasco for URL content extraction and raw HTML fetching."""

from __future__ import annotations

import logging
from dataclasses import replace as dc_replace
from typing import Any

from vasco.cache import Cache
from vasco.config import Config, YouTubeCfg, load_config
from vasco.fetch import fetch_many, fetch_one

log = logging.getLogger(__name__)

_cfg: Config | None = None
_cache: Cache | None = None


def configure(*, cookies_from_browser: str | None = None) -> None:
    global _cfg, _cache
    cfg = load_config()
    if cookies_from_browser:
        cfg = dc_replace(cfg, youtube=YouTubeCfg(cookies_from_browser=cookies_from_browser))
    _cfg = cfg
    _cache = Cache()


def _envelope_ok(env: dict[str, Any]) -> bool:
    return "failure" not in env and bool(env.get("markdown"))


async def fetch_content(url: str, *, refresh: bool = False) -> tuple[str, str | None] | None:
    env = await fetch_one(url, mode="auto", refresh=refresh, cache=_cache, cfg=_cfg)
    if not _envelope_ok(env):
        log.warning("vasco fetch failed for %s: %s", url, env.get("failure", {}).get("message", ""))
        return None
    return env["markdown"], env.get("image")


async def fetch_content_batch(
    urls: list[str],
) -> dict[str, tuple[str, str | None]]:
    results: dict[str, tuple[str, str | None]] = {}
    async for env in fetch_many(urls, workers=4, mode="auto", cache=_cache, cfg=_cfg):
        key = env.get("url_requested", "")
        if _envelope_ok(env):
            results[key] = (env["markdown"], env.get("image"))
        else:
            log.warning(
                "vasco fetch failed for %s: %s",
                key,
                env.get("failure", {}).get("message", ""),
            )
    return results


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
