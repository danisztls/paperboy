"""Thin client bridge to vascod — the resident vasco daemon — over its UNIX socket.

paperboy no longer imports vasco as a library; it sends fetch requests to the
``vascod`` service (`vasco serve`) and reads back the envelope. The public
functions below keep their original signatures — only the transport changed.

Wire protocol mirrors ``vasco/service/protocol.py`` (4-byte big-endian length
prefix + JSON). ``PROTOCOL_VERSION`` is vendored here; a mismatch is logged loudly
so the two repos can't silently drift. The daemon is reached at
``$XDG_RUNTIME_DIR/vasco/vascod.sock`` (overridable via ``VASCO_SERVICE_SOCKET``).

If the daemon is unreachable or returns a failure, these return ``None`` — the
same "skip this fetch" contract callers already handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# MUST match vasco/service/protocol.py PROTOCOL_VERSION. A mismatch is logged.
PROTOCOL_VERSION = 1

_HEADER = struct.Struct("!I")
_CONNECT_TIMEOUT = 2.0
# Generous backstop; the daemon bounds the actual work via its per-op deadline.
_READ_TIMEOUT = 120.0


def _socket_path() -> str:
    override = os.environ.get("VASCO_SERVICE_SOCKET")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return str(Path(runtime) / "vasco" / "vascod.sock")


async def _request(op: str, **params: Any) -> Any | None:
    """Send one op to vascod and return its ``result``.

    Returns ``None`` (logged) if the daemon is unreachable or answers ``ok=false``
    — callers treat that as a skipped fetch.
    """
    url = params.get("url")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(_socket_path()), _CONNECT_TIMEOUT
        )
    except (TimeoutError, OSError) as exc:
        log.warning("vascod unreachable (%s) — skipping %s", exc, url)
        return None
    try:
        payload = json.dumps({"op": op, "params": params}).encode()
        writer.write(_HEADER.pack(len(payload)) + payload)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(_HEADER.size), _READ_TIMEOUT)
        (length,) = _HEADER.unpack(header)
        data = await asyncio.wait_for(reader.readexactly(length), _READ_TIMEOUT)
        resp = json.loads(data)
    except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
        log.warning("vascod request failed (%s) for %s", exc, url)
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    if resp.get("protocol_version") != PROTOCOL_VERSION:
        log.error(
            "vascod protocol mismatch: paperboy=%s daemon=%s — update paperboy",
            PROTOCOL_VERSION,
            resp.get("protocol_version"),
        )
        return None
    if not resp.get("ok"):
        log.warning("vascod error for %s: %s", url, (resp.get("error") or {}).get("message"))
        return None
    return resp.get("result")


def configure() -> None:
    """No-op retained for call-site compatibility (config + cache live in vascod)."""
    return None


def _ok_or_none(env: dict[str, Any] | None, url: str) -> dict[str, Any] | None:
    """Return the envelope if it's a usable success, else None (with a log).

    ``env is None`` means the transport already logged; only log here for a real
    fetch *failure* / thin-content envelope.
    """
    if env is None:
        return None
    if "failure" in env or not env.get("markdown"):
        log.warning(
            "vascod fetch failed for %s: %s",
            url,
            (env.get("failure") or {}).get("message", ""),
        )
        return None
    return env


async def fetch_content(url: str, *, refresh: bool = False) -> tuple[str, str | None] | None:
    env = _ok_or_none(await _request("fetch", url=url, refresh=refresh), url)
    if env is None:
        return None
    return env["markdown"], env.get("image")


async def fetch_content_with_title(
    url: str, *, refresh: bool = False
) -> tuple[str, str, str | None, bool] | None:
    env = _ok_or_none(await _request("fetch", url=url, refresh=refresh), url)
    if env is None:
        return None
    is_youtube = env.get("mode_used") == "youtube"
    return env["markdown"], env.get("title") or "", env.get("image"), is_youtube


async def fetch_raw_html(url: str, *, mode: str = "auto") -> str | None:
    env = _ok_or_none(await _request("fetch", url=url, mode=mode, raw=True), url)
    if env is None:
        return None
    return env["markdown"]


async def search(
    query: str,
    *,
    max_results: int = 10,
    region: str | None = None,
    site: str | None = None,
) -> list[dict] | None:
    """Run a web search via vascod (real DDG/Tavily SERP).

    Returns a list of ``{title, url, snippet}`` dicts, or ``None`` on
    unreachable/failure — callers treat that as "no results this step".
    """
    params: dict[str, Any] = {"query": query, "max_results": max_results}
    if region:
        params["region"] = region
    if site:
        params["site"] = site
    result = await _request("search", **params)
    return result if isinstance(result, list) else None


async def extract(url: str, query: str, *, top: int = 5) -> list[dict] | None:
    """Fetch ``url`` via vascod and return the top-``top`` passages matching ``query``.

    Returns the ranked ``passages`` list (BM25/semantic), or ``None`` on
    unreachable/failure / when the page yields nothing.
    """
    result = await _request("extract", url=url, query=query, top=top)
    if not isinstance(result, dict):
        return None
    passages = result.get("passages")
    return passages if isinstance(passages, list) else None


async def fetch_listings(url: str, *, refresh: bool = False) -> dict | None:
    """Fetch structured real-estate listings via vasco's realestate adapter.

    Returns the full envelope (listings in ``env["quality"]["listings"]``,
    provider display name in ``env["site_name"]``) or ``None`` on failure / when
    the URL wasn't routed to the realestate adapter.
    """
    env = await _request("fetch", url=url, refresh=refresh)
    if not env or "failure" in env:
        if env is not None:
            log.warning(
                "vascod fetch failed for %s: %s",
                url,
                (env.get("failure") or {}).get("message", ""),
            )
        return None
    if env.get("mode_used") != "realestate":
        log.warning(
            "vascod did not route %s to realestate adapter (mode=%s)",
            url,
            env.get("mode_used"),
        )
        return None
    return env
