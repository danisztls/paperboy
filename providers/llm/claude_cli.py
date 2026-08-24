# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""`claude -p` adapter — shell out to the locally-installed Claude Code CLI.

A third LLM provider beside DeepSeek and Gemini. Its point is auth: `claude`
reuses the user's existing Claude Code login (subscription OAuth), so curate /
summarize / research can run on Claude models without provisioning an Anthropic
API key. As of 2026-06-15 programmatic use (`claude -p`) draws from a separate
monthly credit pool metered at API rates — so this is not "free" the way
interactive use is, and the adapter logs `total_cost_usd` per call.

Single-shot per call via `--print`: paperboy drives any agentic loop (curate's
corroboration turns arrive as the `messages` conversation), so the CLI itself runs
with all tools disabled and never takes its own turns. Structured output goes
through the prompt (not `--json-schema`, which is ignored when tools are off) and
the JSON `result` is parsed with Pydantic, mirroring DeepSeek's reasoning path.

Fail-open contract (matches the other adapters): any failure — missing binary,
non-zero exit, timeout, error envelope, unparseable output — returns ``None`` and
the caller skips the item / fails open this run.
"""

import asyncio
import json
import logging
import os
import shutil
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMAdapter, LLMResponse, reasoning_level, timed_call

DEFAULT_MODEL = "sonnet"
CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT = 180.0

# Flags that make `claude -p` a clean, hermetic single shot. All three are needed —
# verified by capturing the actual /v1/messages request body. Together they take a
# call from ~11.7K tokens of injected context down to ~160 (the model then sees only
# our --system-prompt + data, no agentic framing that could skew its reasoning):
#   --safe-mode             strips CLAUDE.md/memory, MCP servers + their instructions,
#                           hooks and plugins, while keeping OAuth (unlike --bare, which
#                           forces an API key). Does NOT strip tool defs or slash commands.
#   --no-session-persistence  don't write the session to disk.
#   --tools ""              removes the built-in tool *definitions* (~29 schemas: Bash,
#                           Edit, Write, Task, WebSearch…). NOTE: --allowedTools "" is the
#                           wrong lever — it only denies *permission* to call them, the
#                           full schemas are still sent and still bias the model.
#   --disable-slash-commands  drops the ~27 slash-command descriptions also otherwise sent.
# (A base "You are a Claude agent…" identity line and a billing header remain in the
# system block — a handful of tokens, not removable without --bare; benign.)
_HERMETIC_FLAGS = (
    "--safe-mode",
    "--no-session-persistence",
    "--tools",
    "",
    "--disable-slash-commands",
)

# Stripped from the child env so `claude` authenticates via the subscription OAuth
# credential. A stray key here would silently bill the API (no included credit) — the
# exact thing this backend exists to avoid. Only an *explicitly configured* api_key
# (deliberate API opt-in) is injected back.
_AUTH_ENV_STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _render(
    prompt: str, instructions: str | None, messages: list[dict] | None
) -> tuple[str | None, str]:
    """Flatten the call into (system, body) — the CLI takes one system + one stdin body.

    Single-shot: system = instructions, body = prompt. Multi-turn (`messages` given,
    which wins over prompt/instructions per the ABC): the leading system turn(s) fold
    into the system prompt; the user/assistant turns render into a deterministic,
    byte-stable body so the system stays an identical cacheable prefix across the
    agentic loop's turns.
    """
    if messages is None:
        return instructions, prompt
    sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
    system = "\n\n".join(sys_parts) if sys_parts else instructions
    blocks = [
        f"{'Assistant' if m.get('role') == 'assistant' else 'User'}: {m['content']}"
        for m in messages
        if m.get("role") != "system"
    ]
    return system, "\n\n".join(blocks)


def _extract_json(text: str) -> str:
    """Best-effort isolate a JSON object from the CLI's text result.

    The model is asked for bare JSON, but defend against stray markdown fences or
    surrounding prose by stripping a ```…``` fence then slicing the outermost braces.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.lstrip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _cache_tokens(usage: dict) -> tuple[int | None, int | None]:
    """Map the CLI usage block to (cache_hit, cache_miss) in DeepSeek/Gemini terms.

    `cache_read_input_tokens` is the hit; everything else processed cold
    (`input_tokens` + `cache_creation_input_tokens`) is the miss.
    """
    hit = usage.get("cache_read_input_tokens")
    uncached = usage.get("input_tokens")
    created = usage.get("cache_creation_input_tokens")
    if uncached is None and created is None:
        return hit, None
    miss = (uncached or 0) + (created or 0)
    return hit, miss


class ClaudeCliAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None) -> None:
        # Auth is resolved at call time (inherited OAuth), so don't read an env var here.
        self._api_key = api_key
        self._bin = shutil.which(CLAUDE_BIN)

    def _build_argv(
        self,
        *,
        model: str,
        system: str | None,
        reasoning: bool | str | dict,
    ) -> list[str]:
        argv = [self._bin, "-p", "--output-format", "json", *_HERMETIC_FLAGS, "--model", model]
        if system:
            argv += ["--system-prompt", system]
        level = reasoning_level(reasoning)
        if level is not None:
            argv += ["--effort", level]
        return argv

    async def _run(
        self, argv: list[str], body: str, timeout: float = DEFAULT_TIMEOUT
    ) -> dict | None:
        if self._bin is None:
            log.error("claude binary not found on PATH — install Claude Code or set its path")
            return None
        env = {k: v for k, v in os.environ.items() if k not in _AUTH_ENV_STRIP}
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=body.encode("utf-8")), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.error("claude -p timed out after %ss", timeout)
            return None
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:500]
            log.error("claude -p exited %s: %s", proc.returncode, detail or "(no stderr)")
            return None
        try:
            envelope = json.loads(out.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error("claude -p produced unparseable output: %s", exc)
            return None
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            log.error(
                "claude -p returned a non-success result: subtype=%r", envelope.get("subtype")
            )
            return None
        cost = envelope.get("total_cost_usd")
        if cost is not None:
            log.info("claude_cli call: $%.4f (model=%s)", cost, argv[argv.index("--model") + 1])
        return envelope

    def _response(self, envelope: dict, model: str, elapsed: float) -> LLMResponse | None:
        text = (envelope.get("result") or "").strip()
        if not text:
            return None
        usage = envelope.get("usage") or {}
        cache_hit, cache_miss = _cache_tokens(usage)
        total_input = (
            (cache_hit or 0) + (cache_miss or 0)
            if (cache_hit is not None or cache_miss is not None)
            else None
        )
        ms = envelope.get("duration_ms")
        return LLMResponse(
            text=text,
            model=envelope.get("model") or model,
            input_tokens=total_input,
            output_tokens=usage.get("output_tokens"),
            latency_s=ms / 1000 if ms is not None else elapsed,
            reasoning=None,
            finish_reason=envelope.get("stop_reason") or envelope.get("subtype"),
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
        )

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        messages: list[dict] | None = None,
        reasoning: bool | str | dict = False,
    ) -> LLMResponse | None:
        _model = model or DEFAULT_MODEL
        system, body = _render(prompt, instructions, messages)
        argv = self._build_argv(model=_model, system=system, reasoning=reasoning)
        envelope, elapsed = await timed_call(log, "ClaudeCode", lambda: self._run(argv, body))
        if envelope is None:
            return None
        return self._response(envelope, _model, elapsed)

    async def complete_structured(
        self,
        prompt: str,
        response_model: type[T],
        *,
        model: str | None = None,
        instructions: str | None = None,
        messages: list[dict] | None = None,
        reasoning: bool | str | dict = False,
        trace: dict | None = None,
    ) -> T | None:
        _model = model or DEFAULT_MODEL
        system, body = _render(prompt, instructions, messages)
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        # Schema goes on the body (the system stays the cache-stable prefix across the
        # loop's differently-typed turns), and --json-schema is intentionally not used.
        body = (
            f"{body}\n\nRespond with ONLY a single JSON object matching this schema "
            f"(no markdown fences, no commentary):\n{schema}"
        )
        argv = self._build_argv(model=_model, system=system, reasoning=reasoning)
        envelope, elapsed = await timed_call(log, "ClaudeCode", lambda: self._run(argv, body))
        if envelope is None:
            return None
        try:
            parsed = response_model.model_validate_json(_extract_json(envelope.get("result") or ""))
        except ValidationError as exc:
            log.warning("claude_cli structured response failed validation: %s", exc)
            return None
        if trace is not None:
            resp = self._response(envelope, _model, elapsed)
            usage = envelope.get("usage") or {}
            cache_hit, cache_miss = _cache_tokens(usage)
            trace["latency_s"] = resp.latency_s if resp else elapsed
            trace["model_used"] = envelope.get("model") or _model
            trace["input_tokens"] = resp.input_tokens if resp else None
            trace["output_tokens"] = usage.get("output_tokens")
            trace["reasoning"] = None
            trace["cache_hit_tokens"] = cache_hit
            trace["cache_miss_tokens"] = cache_miss
            trace["cost_usd"] = envelope.get("total_cost_usd")
        return parsed
