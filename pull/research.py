"""Agentic web-research task.

Drives vasco's real search + fetch/extract (over vascod) in a bounded loop:
each turn the LLM picks the next action (search / read / finish) via structured
output, claudinho executes it against vascod and feeds the result back, then the
LLM synthesizes a final answer over the gathered passages. This replaces the old
provider-`web_search` search task — retrieval now comes from vasco (cache,
escalation, quality scoring), not the model's built-in tool.

Never raises: vascod failures surface as ``None`` (treated as "no results"); an
LLM failure ends the loop early and we synthesize from whatever was gathered.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from config import get_research_cfg
from process import _vasco
from providers.llm.base import LLMAdapter

log = logging.getLogger(__name__)


class ResearchAction(BaseModel):
    """The agent's next move, chosen each turn via structured output."""

    rationale: str = Field(description="One short sentence: why this action now.")
    kind: Literal["search", "read", "finish"]
    queries: list[str] = Field(
        default_factory=list, description="Search queries to run when kind='search'."
    )
    urls: list[str] = Field(
        default_factory=list, description="Candidate URLs to read when kind='read'."
    )


@dataclass
class ResearchState:
    """Accumulated knowledge across the loop."""

    searched: set[str] = field(default_factory=set)  # queries already issued
    seen: set[str] = field(default_factory=set)  # urls already read
    candidates: dict[str, dict] = field(default_factory=dict)  # url -> {title, snippet}
    sources: dict[str, dict] = field(default_factory=dict)  # url -> {title, passages}


_DECISION_SYSTEM = (
    "You are a web-research agent. Your goal is to gather enough information to fulfill the "
    "user's request, then stop. Two tools are run by the system between your turns:\n"
    "- search(query): runs a real web search, returns result URLs with titles/snippets.\n"
    "- read(url): fetches a URL and returns the passages most relevant to the request.\n\n"
    "Each turn choose ONE action:\n"
    "- kind='search' with one or more `queries` to discover sources;\n"
    "- kind='read' with one or more `urls` taken from the candidates list to extract content;\n"
    "- kind='finish' once the gathered passages are enough to answer well.\n\n"
    "Prefer reading promising candidates over searching endlessly. Never repeat a query or "
    "re-read a URL. Be efficient — your steps, searches, and reads are limited."
)

_SYNTHESIS_SYSTEM = (
    "You are a research assistant. Using ONLY the numbered sources provided, write a clear, "
    "well-organized answer to the user's request. "
    "Be ruthlessly selective: include only items that materially matter to the request and "
    "omit marginal, low-impact, speculative, or purely narrative ones entirely. Prefer a few "
    "high-signal items over a long list. Report concrete facts, not opinion, mood, or "
    "political color. Do not pad — if little is genuinely significant, keep it short. Honor "
    "any KEEP/DISCARD or selectivity rules in the request. "
    "Cite sources inline as [n] using the same numbers shown in the sources list (the "
    "bracketed number alone — the system turns each [n] into a clickable link). Do NOT write "
    "URLs yourself and do NOT add a separate sources/references section. If the sources are "
    "insufficient, say so plainly. No filler or meta-commentary."
)

# Matches a citation marker: one or more comma-separated numbers in brackets, e.g.
# "[1]", "[1, 2]". Adjacent markers like "[1][2]" match separately.
_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _linkify_citations(text: str, url_by_num: dict[int, str]) -> str:
    """Turn inline citation markers `[n]` into digest-style links `[[n](url)]`.

    Mirrors the digest citation form (`push.discord._render_paragraph` emits
    `[[label](url)]`). Uses the same numbering the model was shown; `[1, 2]`
    expands to two space-separated links; a number with no known URL is left as a
    plain `[n]`. URLs are emitted plain — Discord preview-suppression (`<...>`) and
    wrap-protection are applied later by `push.discord.post_text_to_discord`, the
    same path the digest tasks use.
    """

    def repl(m: re.Match[str]) -> str:
        nums = [int(x) for x in re.split(r"\s*,\s*", m.group(1))]
        parts = [(f"[[{k}]({url_by_num[k]})]" if k in url_by_num else f"[{k}]") for k in nums]
        return " ".join(parts)

    return _CITATION_RE.sub(repl, text)


def _passage_text(p) -> str:
    if isinstance(p, dict):
        return str(p.get("text") or p.get("passage") or "").strip()
    return str(p).strip()


def _state_view(prompt: str, state: ResearchState, *, steps_left, searches_left, reads_left) -> str:
    lines = [
        f"Request: {prompt}",
        "",
        f"Budget: steps_left={steps_left}, searches_left={searches_left}, reads_left={reads_left}",
    ]
    if state.searched:
        lines += ["", "Queries already run: " + "; ".join(sorted(state.searched))]
    if state.sources:
        lines.append("\nSources already READ (url — title — first passage):")
        for url, s in state.sources.items():
            passages = s.get("passages") or []
            first = _passage_text(passages[0]) if passages else "(no passages)"
            lines.append(f"- {url} — {s.get('title') or ''} — {first[:200]}")
    unread = [u for u in state.candidates if u not in state.seen]
    if unread:
        lines.append("\nCandidate URLs found but NOT yet read:")
        for url in unread:
            c = state.candidates[url]
            lines.append(f"- {url} — {c.get('title') or ''} — {(c.get('snippet') or '')[:160]}")
    if not (state.searched or state.candidates or state.sources):
        lines.append("\n(nothing gathered yet — start by searching)")
    return "\n".join(lines)


async def run_research_task(
    task_cfg: dict,
    instructions: str | None = None,
    global_model: str | None = None,
    *,
    adapter: LLMAdapter,
    reasoning: bool | str | dict = False,
    trace: dict | None = None,
) -> str | None:
    """Run the bounded agentic research loop. Returns the synthesized answer or None.

    The per-task `model.reasoning` override is honored only when the caller's
    `reasoning` is falsy (matching the curate/search convention).
    """
    rcfg = get_research_cfg(task_cfg)
    prompt = rcfg["prompt"]
    raw_model = rcfg.get("model")
    model = (raw_model.get("name") if isinstance(raw_model, dict) else None) or global_model or None
    if not reasoning and isinstance(raw_model, dict) and raw_model.get("reasoning"):
        reasoning = raw_model["reasoning"]
    task_instructions = rcfg.get("instructions")
    combined_instructions = "\n\n".join(filter(None, [instructions, task_instructions])) or None

    max_steps = int(rcfg.get("max_steps", 6))
    max_searches = int(rcfg.get("max_searches", 3))
    max_reads = int(rcfg.get("max_reads", 6))
    max_results = int(rcfg.get("max_results", 8))
    read_top = int(rcfg.get("read_top", 5))

    name = task_cfg.get("name")
    state = ResearchState()
    steps_log: list[dict] = []
    searches_done = 0
    reads_done = 0

    decision_system = _DECISION_SYSTEM
    if combined_instructions:
        decision_system += f"\n\nAdditional guidance for this task:\n{combined_instructions}"

    for step in range(max_steps):
        view = _state_view(
            prompt,
            state,
            steps_left=max_steps - step,
            searches_left=max_searches - searches_done,
            reads_left=max_reads - reads_done,
        )
        action = await adapter.complete_structured(
            view,
            ResearchAction,
            model=model,
            instructions=decision_system,
            reasoning=reasoning,
        )
        if action is None:
            log.warning("[%s] research: decision step %d returned None — stopping", name, step)
            break
        steps_log.append(
            {
                "step": step,
                "kind": action.kind,
                "rationale": action.rationale,
                "queries": list(action.queries),
                "urls": list(action.urls),
            }
        )
        log.debug("[%s] research step %d: %s — %s", name, step, action.kind, action.rationale)

        if action.kind == "finish":
            break
        if action.kind == "search":
            for q in action.queries:
                if searches_done >= max_searches:
                    break
                q = (q or "").strip()
                if not q or q in state.searched:
                    continue
                state.searched.add(q)
                searches_done += 1
                results = await _vasco.search(q, max_results=max_results)
                for r in results or []:
                    url = r.get("url")
                    if not url or url in state.seen or url in state.candidates:
                        continue
                    state.candidates[url] = {"title": r.get("title"), "snippet": r.get("snippet")}
        elif action.kind == "read":
            for url in action.urls:
                if reads_done >= max_reads:
                    break
                url = (url or "").strip()
                if not url or url in state.seen:
                    continue
                state.seen.add(url)
                reads_done += 1
                passages = await _vasco.extract(url, prompt, top=read_top)
                title = (state.candidates.get(url) or {}).get("title")
                state.sources[url] = {"title": title, "passages": passages or []}
                state.candidates.pop(url, None)

        if searches_done >= max_searches and reads_done >= max_reads:
            log.debug("[%s] research: search/read budgets exhausted — synthesizing", name)
            break

    answer = await _synthesize(
        adapter,
        prompt,
        state,
        model=model,
        instructions=combined_instructions,
        reasoning=reasoning,
    )
    if trace is not None:
        trace["model"] = model
        trace["model_used"] = model
        trace["instructions"] = combined_instructions
        trace["prompt"] = prompt
        trace["steps"] = steps_log
        trace["sources"] = [{"url": u, "title": s.get("title")} for u, s in state.sources.items()]
        trace["answer"] = answer
    if not answer:
        log.warning("[%s] research produced no answer", name)
    return answer


async def _synthesize(
    adapter: LLMAdapter,
    prompt: str,
    state: ResearchState,
    *,
    model: str | None,
    instructions: str | None,
    reasoning: bool | str | dict,
) -> str | None:
    blocks: list[str] = []
    url_by_num: dict[int, str] = {}  # citation number -> url, for inline linkification
    n = 0

    def _add(url: str, title: str, body: str) -> None:
        nonlocal n
        n += 1
        blocks.append(f"[{n}] {title}\nURL: {url}\n{body}")
        url_by_num[n] = url

    for url, s in state.sources.items():
        body = "\n".join(t for p in s.get("passages", []) if (t := _passage_text(p)))
        if body:
            _add(url, s.get("title") or url, body)
    # If little was read, fall back to unread candidate snippets as weaker sources.
    if len(blocks) < 2:
        for url, c in state.candidates.items():
            snip = (c.get("snippet") or "").strip()
            if snip:
                _add(url, c.get("title") or url, snip)
    sources_text = "\n\n".join(blocks) if blocks else "(no sources were gathered)"

    system = _SYNTHESIS_SYSTEM
    if instructions:
        system += f"\n\nAdditional guidance:\n{instructions}"
    user = f"Request: {prompt}\n\nSources:\n\n{sources_text}"
    resp = await adapter.complete(user, model=model, instructions=system, reasoning=reasoning)
    if resp is None or not resp.text:
        return None
    # Inline-link the citations: [n] -> [[n](url)] (digest citation style).
    # post_text_to_discord applies the same embed-suppression + wrap-protection.
    return _linkify_citations(resp.text.strip(), url_by_num)
