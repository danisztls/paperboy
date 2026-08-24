# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests for the claude_cli adapter (providers/llm/claude_cli.py).

The subprocess boundary is faked: `shutil.which` is stubbed so the binary
"exists", and `asyncio.create_subprocess_exec` returns a `_FakeProc`. No real
`claude` call is ever made — the suite asserts on the argv we build, the stdin we
feed, the env we pass, and how we parse the JSON envelope.
"""

import json
from typing import Any

import pytest
from pydantic import BaseModel

from providers.llm import claude_cli as cc


@pytest.fixture(autouse=True)
def _binary_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda _b: "/usr/bin/claude")


class _Decision(BaseModel):
    verdict: str
    score: int


class _FakeProc:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.stdin_input: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_input = input
        if self._hang:
            import asyncio

            await asyncio.sleep(10)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _patch_exec(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> dict[str, Any]:
    captured: dict[str, Any] = {"spawned": False}

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["spawned"] = True
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _envelope(result: str, **extra: Any) -> bytes:
    base = {"type": "result", "subtype": "success", "is_error": False, "result": result}
    base.update(extra)
    return json.dumps(base).encode()


def _full_usage_envelope(result: str) -> bytes:
    return _envelope(
        result,
        stop_reason="end_turn",
        duration_ms=1500,
        total_cost_usd=0.0231,
        usage={
            "input_tokens": 2,
            "cache_read_input_tokens": 2946,
            "cache_creation_input_tokens": 100,
            "output_tokens": 42,
        },
    )


# --- complete() ---


async def test_complete_happy_path(monkeypatch):
    proc = _FakeProc(stdout=_full_usage_envelope("  Hello there  "))
    _patch_exec(monkeypatch, proc)
    resp = await cc.ClaudeCliAdapter().complete("hi", model="sonnet")

    assert resp.text == "Hello there"  # trimmed
    assert resp.model == "sonnet"  # no top-level model → falls back to requested
    assert resp.input_tokens == 2946 + (2 + 100)  # hit + miss = total prompt
    assert resp.output_tokens == 42
    assert resp.cache_hit_tokens == 2946
    assert resp.cache_miss_tokens == 102
    assert resp.latency_s == 1.5  # duration_ms / 1000
    assert resp.finish_reason == "end_turn"  # stop_reason preferred over subtype


async def test_default_model_when_unspecified(monkeypatch):
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    resp = await cc.ClaudeCliAdapter().complete("hi")
    assert resp.model == cc.DEFAULT_MODEL
    assert captured["args"][captured["args"].index("--model") + 1] == cc.DEFAULT_MODEL


async def test_hermetic_flags_and_stdin(monkeypatch):
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliAdapter().complete("the prompt body", model="opus")

    args = captured["args"]
    assert args[0] == "/usr/bin/claude"
    assert "-p" in args
    assert args[args.index("--output-format") + 1] == "json"
    assert "--safe-mode" in args
    assert "--no-session-persistence" in args
    # tool defs + slash commands stripped (clean context); --tools "" is the real lever,
    # NOT --allowedTools (which only denies permission, leaving the schemas in-context).
    assert args[args.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in args
    assert "--allowedTools" not in args
    assert args[args.index("--model") + 1] == "opus"
    assert "--json-schema" not in args
    # prompt rides stdin, never the argv
    assert proc.stdin_input == b"the prompt body"
    assert "the prompt body" not in args


async def test_system_prompt_only_when_instructions(monkeypatch):
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliAdapter().complete("p")
    assert "--system-prompt" not in captured["args"]

    proc2 = _FakeProc(stdout=_envelope("ok"))
    captured2 = _patch_exec(monkeypatch, proc2)
    await cc.ClaudeCliAdapter().complete("p", instructions="be terse")
    assert captured2["args"][captured2["args"].index("--system-prompt") + 1] == "be terse"


@pytest.mark.parametrize(
    "reasoning,expected",
    [("low", "low"), ("medium", "medium"), ("high", "high"), (True, "high")],
)
async def test_effort_present(monkeypatch, reasoning, expected):
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliAdapter().complete("p", reasoning=reasoning)
    assert captured["args"][captured["args"].index("--effort") + 1] == expected


@pytest.mark.parametrize("reasoning", [False, "off"])
async def test_effort_absent(monkeypatch, reasoning):
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliAdapter().complete("p", reasoning=reasoning)
    assert "--effort" not in captured["args"]


async def test_messages_path_renders_to_system_and_body(monkeypatch):
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    # messages must win over a passed prompt/instructions
    await cc.ClaudeCliAdapter().complete(
        "IGNORED PROMPT", instructions="IGNORED SYS", messages=messages
    )
    args = captured["args"]
    assert args[args.index("--system-prompt") + 1] == "SYS"
    body = proc.stdin_input.decode()
    assert body == "User: first\n\nAssistant: reply\n\nUser: second"
    assert "IGNORED PROMPT" not in body


# --- complete_structured() ---


async def test_complete_structured_happy_path_and_trace(monkeypatch):
    proc = _FakeProc(stdout=_full_usage_envelope(json.dumps({"verdict": "keep", "score": 3})))
    captured = _patch_exec(monkeypatch, proc)
    trace: dict = {}
    out = await cc.ClaudeCliAdapter().complete_structured(
        "classify this", _Decision, model="sonnet", trace=trace
    )
    assert out == _Decision(verdict="keep", score=3)
    # no --json-schema; schema rides the stdin body
    assert "--json-schema" not in captured["args"]
    assert "JSON object matching this schema" in proc.stdin_input.decode()
    assert "classify this" in proc.stdin_input.decode()
    # trace mirrors the deepseek key set
    assert trace["latency_s"] == 1.5
    assert trace["model_used"] == "sonnet"
    assert trace["input_tokens"] == 2946 + 102
    assert trace["output_tokens"] == 42
    assert trace["reasoning"] is None
    assert trace["cache_hit_tokens"] == 2946
    assert trace["cache_miss_tokens"] == 102
    assert trace["cost_usd"] == 0.0231


async def test_complete_structured_strips_fences(monkeypatch):
    fenced = '```json\n{"verdict": "drop", "score": 0}\n```'
    proc = _FakeProc(stdout=_envelope(fenced))
    _patch_exec(monkeypatch, proc)
    out = await cc.ClaudeCliAdapter().complete_structured("p", _Decision)
    assert out == _Decision(verdict="drop", score=0)


async def test_complete_structured_extracts_from_prose(monkeypatch):
    prose = 'Here is the result: {"verdict": "keep", "score": 2} — done.'
    proc = _FakeProc(stdout=_envelope(prose))
    _patch_exec(monkeypatch, proc)
    out = await cc.ClaudeCliAdapter().complete_structured("p", _Decision)
    assert out == _Decision(verdict="keep", score=2)


async def test_complete_structured_validation_failure_returns_none(monkeypatch):
    proc = _FakeProc(stdout=_envelope('{"verdict": "keep"}'))  # missing required score
    _patch_exec(monkeypatch, proc)
    out = await cc.ClaudeCliAdapter().complete_structured("p", _Decision)
    assert out is None


# --- fail-open paths ---


async def test_nonzero_exit_returns_none(monkeypatch):
    proc = _FakeProc(stdout=b"", stderr=b"boom", returncode=1)
    _patch_exec(monkeypatch, proc)
    assert await cc.ClaudeCliAdapter().complete("p") is None


async def test_unparseable_output_returns_none(monkeypatch):
    proc = _FakeProc(stdout=b"not json at all")
    _patch_exec(monkeypatch, proc)
    assert await cc.ClaudeCliAdapter().complete("p") is None


async def test_is_error_returns_none(monkeypatch):
    proc = _FakeProc(stdout=_envelope("", is_error=True, subtype="error_during_execution"))
    _patch_exec(monkeypatch, proc)
    assert await cc.ClaudeCliAdapter().complete("p") is None


async def test_non_success_subtype_returns_none(monkeypatch):
    proc = _FakeProc(stdout=json.dumps({"subtype": "error_max_turns", "result": "x"}).encode())
    _patch_exec(monkeypatch, proc)
    assert await cc.ClaudeCliAdapter().complete("p") is None


async def test_empty_result_returns_none(monkeypatch):
    proc = _FakeProc(stdout=_envelope("   "))
    _patch_exec(monkeypatch, proc)
    assert await cc.ClaudeCliAdapter().complete("p") is None


async def test_timeout_kills_and_returns_none(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    adapter = cc.ClaudeCliAdapter()
    out = await adapter._run(["x"], "body", timeout=0.01)
    assert out is None
    assert proc.killed is True


async def test_missing_binary_never_spawns(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda _b: None)
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    assert await cc.ClaudeCliAdapter().complete("p") is None
    assert captured["spawned"] is False


# --- env / auth ---


async def test_env_strips_auth_by_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("PATH", "/usr/bin")
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliAdapter().complete("p")

    env = captured["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env  # billed to subscription, not the API
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin"  # rest of env preserved


async def test_explicit_api_key_is_injected(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    proc = _FakeProc(stdout=_envelope("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliAdapter(api_key="sk-explicit").complete("p")
    assert captured["kwargs"]["env"]["ANTHROPIC_API_KEY"] == "sk-explicit"


# --- _extract_json unit ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('prefix {"a": 1} suffix', '{"a": 1}'),
    ],
)
def test_extract_json(raw, expected):
    assert json.loads(cc._extract_json(raw)) == json.loads(expected)
