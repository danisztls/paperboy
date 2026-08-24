# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

"""Render checks for the --analysis (--human) views."""

import io

from rich.console import Console

from evals.capture import LLMCall, TaskCapture
from evals.render import render_run


def _render(call: LLMCall) -> str:
    task = TaskCapture(task="t", type="digest", timestamp="2026-06-15T00:00:00Z", calls=[call])
    buf = io.StringIO()
    render_run([task], console=Console(file=buf, width=140))
    return buf.getvalue()


def test_filter_render_shows_corroboration_trajectory_and_cache():
    """The agentic path surfaces what it searched, the cache hit ratio, and reasoning size."""
    call = LLMCall(
        task="t",
        call_type="filter",
        ts="2026-06-15T00:00:00Z",
        model="deepseek-v4-pro",
        cache_hit_tokens=900,
        cache_miss_tokens=100,
        reasoning="z" * 1234,
        steps=[
            {
                "step": 0,
                "kind": "search",
                "rationale": "verify the strike happened",
                "queries": ["us strike venezuela"],
                "results": [
                    {
                        "query": "us strike venezuela",
                        "hits": [
                            {
                                "title": "Reuters confirms strike",
                                "snippet": "...",
                                "url": "https://r.com",
                            }
                        ],
                    }
                ],
            },
            {"step": 1, "kind": "finish", "rationale": "enough", "queries": []},
        ],
        payload=[{"source": "AP", "items": [{"id": 0}]}],
        parsed=[
            {
                "id": 0,
                "source": "AP",
                "title": "Strike confirmed",
                "url": "https://a.com",
                "pass": True,
                "reason": "kept",
            }
        ],
        memory="briefing",
    )
    out = _render(call)
    assert "corroboration" in out
    assert "us strike venezuela" in out  # the searched query
    assert "Reuters confirms strike" in out  # a hit title
    assert "cache=900/1000 (90%)" in out
    assert "reasoning: 1234 chars" in out


def test_filter_render_without_corroboration_omits_trajectory():
    """Standard (non-agentic) curate has no steps — no corroboration block, no cache line."""
    call = LLMCall(
        task="t",
        call_type="filter",
        ts="2026-06-15T00:00:00Z",
        model="deepseek-v4-pro",
        payload=[{"source": "AP", "items": [{"id": 0}]}],
        parsed=[
            {"id": 0, "source": "AP", "title": "x", "url": "u", "pass": False, "reason": "dropped"}
        ],
    )
    out = _render(call)
    assert "corroboration" not in out
    assert "cache=" not in out
