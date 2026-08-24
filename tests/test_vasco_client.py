# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Contract test for the vascod thin client (process/_vasco.py).

Self-contained: spins up a tiny stub daemon that speaks the wire protocol, so it
validates paperboy's half of the contract without importing vasco (which
paperboy no longer depends on). Cross-repo PROTOCOL_VERSION agreement is enforced
at runtime by the client's mismatch guard, exercised here too.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from process import _vasco

_HEADER = struct.Struct("!I")


@asynccontextmanager
async def _stub_daemon(sock: Path, responder: Callable[[dict], dict]):
    """Serve one canned response per request over the length-prefixed protocol."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    hdr = await reader.readexactly(_HEADER.size)
                except asyncio.IncompleteReadError:
                    break
                (n,) = _HEADER.unpack(hdr)
                req = json.loads(await reader.readexactly(n))
                payload = json.dumps(responder(req)).encode()
                writer.write(_HEADER.pack(len(payload)) + payload)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(_handle, path=str(sock))
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


def _ok(result: dict, *, version: int = _vasco.PROTOCOL_VERSION) -> Callable[[dict], dict]:
    return lambda req: {"protocol_version": version, "ok": True, "result": result}


async def test_fetch_content_maps_markdown_and_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "vascod.sock"
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))

    captured: dict[str, Any] = {}

    def responder(req: dict) -> dict:
        captured.update(req)
        return _ok({"markdown": "# Hi", "image": "https://img", "mode_used": "http"})(req)

    async with _stub_daemon(sock, responder):
        out = await _vasco.fetch_content("https://x.test", refresh=True)

    assert out == ("# Hi", "https://img")
    assert captured["op"] == "fetch"
    assert captured["params"] == {"url": "https://x.test", "refresh": True}


async def test_fetch_content_with_title_detects_youtube(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "vascod.sock"
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))
    resp = _ok({"markdown": "transcript", "title": "Vid", "mode_used": "youtube"})

    async with _stub_daemon(sock, resp):
        out = await _vasco.fetch_content_with_title("https://youtu.be/x")

    assert out == ("transcript", "Vid", None, True)


async def test_fetch_raw_html_requests_raw_and_returns_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "vascod.sock"
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))
    captured: dict[str, Any] = {}

    def responder(req: dict) -> dict:
        captured.update(req)
        return _ok({"markdown": "<html>raw</html>", "mode_used": "http"})(req)

    async with _stub_daemon(sock, responder):
        out = await _vasco.fetch_raw_html("https://x.test", mode="browser")

    assert out == "<html>raw</html>"
    assert captured["params"] == {"url": "https://x.test", "mode": "browser", "raw": True}


async def test_fetch_listings_requires_realestate_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "vascod.sock"
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))

    realestate = _ok(
        {"markdown": "x", "mode_used": "realestate", "quality": {"listings": [{"url": "u"}]}}
    )
    async with _stub_daemon(sock, realestate):
        env = await _vasco.fetch_listings("https://vivareal.com.br/aluguel/")
    assert env is not None and env["quality"]["listings"] == [{"url": "u"}]

    # A non-realestate route → None.
    plain = _ok({"markdown": "x", "mode_used": "http"})
    async with _stub_daemon(sock, plain):
        assert await _vasco.fetch_listings("https://example.com") is None


async def test_failure_envelope_maps_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "vascod.sock"
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))
    fail = _ok({"url_requested": "https://x.test", "failure": {"reason": "not_found"}})

    async with _stub_daemon(sock, fail):
        assert await _vasco.fetch_content("https://x.test") is None


async def test_protocol_version_mismatch_maps_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "vascod.sock"
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))
    wrong = _ok({"markdown": "x"}, version=_vasco.PROTOCOL_VERSION + 999)

    async with _stub_daemon(sock, wrong):
        assert await _vasco.fetch_content("https://x.test") is None


async def test_daemon_unreachable_maps_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No server listening at this path.
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(tmp_path / "nope.sock"))
    assert await _vasco.fetch_content("https://x.test") is None
