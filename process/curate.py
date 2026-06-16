import asyncio
import json
import logging
import textwrap
from dataclasses import replace as dc_replace
from typing import Literal

from pydantic import BaseModel, Field

from pipeline import Citation, CoverageUpdate, CurateResult, Item
from providers.llm.base import LLMAdapter, ModelHandle

log = logging.getLogger(__name__)


class FilterItem(BaseModel):
    """One item's filter verdict. `passes` is the JSON field `pass` for LLM compatibility."""

    id: int
    passes: bool
    reason: str


class CoverageItem(BaseModel):
    """One topic the curator covered this run (structured-output shape).

    `continues` ties it to an existing coverage-ledger topic (or null = new topic);
    `state` is the latest factual state and doubles as the digest briefing paragraph.
    """

    continues: str | None = None
    label: str
    section: str | None = None
    state: str
    citations: list[int] = []


class FilterDecisions(BaseModel):
    """Structured-output shape produced by the LLM curate call."""

    items: list[FilterItem]
    coverage: list[CoverageItem] = []


class CurateAction(BaseModel):
    """One turn of the agentic corroboration loop: search the web, or finish."""

    kind: Literal["search", "finish"]
    rationale: str
    queries: list[str] = Field(
        default_factory=list, description="Queries to run when kind='search'."
    )


_SEARCH_PREAMBLE = textwrap.dedent("""\
    ## Corroboration tool

    Before delivering verdicts you may search the web to check facts you are unsure
    about. Your training is months out of date, so use search to:
    - verify whether a claim is corroborated by independent reporting (credibility), and
    - check the CURRENT state of an arrangement before judging whether an event breaks
      it (structural dissonance).

    Each turn, respond with a CurateAction: `kind='search'` with a list of `queries`
    (only for the few items you are genuinely uncertain about — do not search items you
    can already judge), or `kind='finish'` when you have enough to decide. Your search
    budget is limited. After you finish you will be asked for the final verdicts over
    ALL items.
    """)


def _resolve_model_reasoning(filter_cfg, global_model, reasoning):
    """Resolve the effective model name + reasoning (per-task curate.model override)."""
    raw_model = filter_cfg.get("model")
    model = (raw_model.get("name") if isinstance(raw_model, dict) else None) or global_model or None
    if not reasoning and isinstance(raw_model, dict) and raw_model.get("reasoning"):
        reasoning = raw_model["reasoning"]
    return model, reasoning


def _format_ledger(ledger: list[dict]) -> str:
    """Render the coverage ledger as compact lines the model can match + reference."""
    lines = []
    for e in ledger:
        lines.append(
            f"- id={e['id']} | freq={e.get('frequency', 1)} | last={str(e.get('last_seen', ''))[:10]} | {e.get('label', '')}\n"
            f"    {e.get('state', '')}"
        )
    return "\n".join(lines)


def _format_rollups(rollups: list[dict]) -> str:
    """Render aged month rollups as compact background lines (period → top topic labels)."""
    lines = []
    for r in rollups:
        topics = "; ".join(t.get("label", "") for t in r.get("topics", []) if t.get("label"))
        lines.append(f"- {r.get('period', '?')}: {topics}")
    return "\n".join(lines)


def _build_curate_instructions(
    filter_cfg: dict,
    *,
    language: str,
    ledger: list[dict] | None,
    rollups: list[dict] | None = None,
    extra_instructions: str | None,
    explain: bool,
) -> str:
    """Build the full curate instructions (criteria + coverage ledger + Step 1/2/3 body)."""
    criteria = filter_cfg.get("criteria", "")
    prefix = f"## Filter criteria\n{criteria}\n\n"
    if extra_instructions:
        prefix += f"## Additional instructions\n{extra_instructions}\n\n"
    if ledger:
        prefix += (
            "## Coverage ledger (topics already sent to readers — DO NOT re-report)\n"
            "Each line is a topic already covered: `id` (reference it when continuing the topic), "
            "`freq` (how many times it has been covered), `last` (date last covered), the label, and "
            "its latest known state. Use the ledger for Step 2 (dedup + escalating-trajectory) and "
            "Step 3 (continuing vs new topics). Never cite a ledger topic as a source.\n\n"
            + _format_ledger(ledger)
            + "\n\n"
        )
    if rollups:
        prefix += (
            "## Background — older coverage (context only, NOT for dedup)\n"
            "Major topics your feeds covered in earlier months (most recent first). Use this "
            "only as background for judging significance and for recognising a long-dormant "
            "topic that resurfaces — do NOT dedup or apply the trajectory bar against it.\n\n"
            + _format_rollups(rollups)
            + "\n\n"
        )

    reason_format = (
        "Reason format — PASSING items: 2–3 sentence plain-language explanation of what the story is about and why it is relevant, written for someone with no prior context (ELI5 style).\nReason format — FAILING items: one short sentence explaining why it was excluded."
        if explain
        else 'Keep the "reason" field to one short sentence for all items.'
    )

    body = textwrap.dedent(f"""\
        ## Input

        You will receive a JSON array of source groups, each with a 'source' name and an 'items' array. Each item has an integer 'id', a 'title', and an optional 'description'.

        ## Steps

        **Step 1 — Filter.** For each item, decide whether it matches the filter criteria above. Mark it passes: true if it does, passes: false otherwise.

        **Step 2 — Deduplicate against the coverage ledger.**
        - Match each item to a ledger topic by subject. Fail any item whose topic is already in the ledger and that does not ADVANCE that topic's state with a significant new development (new facts, updated numbers, a meaningful consequence). Use reason: 'already covered'. Readers must never see the same topic twice without a genuine update.
        - Escalating-trajectory bar: read the topic's `freq` directly — the higher the `freq`, the higher the bar for another instalment. At freq 1 a concrete update may pass; at a high `freq`, mere incremental movement (another number, another routine step, another day of the same trend) is 'more of the same' and should fail with reason: 'trajectory already covered'. Pass only when the development changes the reader's picture: a reversal, a resolution, a turning point, a newly-realised consequence, or a structural rupture.
        - Within this batch, if multiple items cover the same event, keep only the one(s) that contribute the most relevant information; fail the rest with reason: 'duplicate within batch'.

        **Step 3 — Update coverage.** For EVERY passing item, emit one `coverage` entry for the topic it covers, in {language}. Each entry has:
        - `continues`: the `id` of the ledger topic this item continues, or `null` if it introduces a NEW topic not in the ledger.
        - `label`: a short, canonical topic label that stays STABLE across runs so future instalments can be matched to it (e.g. "US–Iran war & ceasefire", not the headline).
        - `section`: short thematic heading (e.g. "Brasil", "Geopolítica", "Economia"). Set ONLY on the first entry of a new thematic group; `null` otherwise. Never put the section inside `state`.
        - `state`: 1–3 sentences giving the latest factual state of the topic. Lead with the core fact; add only the most essential detail (key figure, number, date, place, consequence). This text is shown to readers — no citation markers, brackets, section names, or meta-commentary. When continuing a ledger topic, write what is NEW, not a restatement.
        - `citations`: list of integer item IDs from THIS batch supporting the entry. Usually one; use multiple only when two items genuinely cover the same event.

        Rules:
        - One entry per passing topic; if two passing items cover the same topic, merge into ONE entry citing both. Emit nothing for failing items.
        - Group by theme via `section`; within each group order by significance; lead with the single most significant development overall.
        - No meta-commentary about the filtering process, no mention of what was discarded, no hedging.
        - Include enough specificity (names, numbers, dates, places) that the next run can recognise a continuation.

        ## Output

        Include ALL input items in `items`, both passing and failing. Populate `coverage` with one entry per passing topic per Step 3.

        {reason_format}
    """)
    return prefix + body


def _parse_decisions(decisions, prefix_tag, total, trace):
    """Decode a FilterDecisions into (results_dict, coverage) and log the pass count."""
    if trace is not None:
        trace["raw_response"] = decisions.model_dump_json()
    parsed = {
        str(item.id): {"pass": item.passes, "reason": item.reason} for item in decisions.items
    }
    coverage = [
        CoverageUpdate(
            continues=c.continues,
            label=c.label,
            state=c.state,
            citations=c.citations,
            section=c.section,
        )
        for c in decisions.coverage
    ]
    passed = sum(1 for v in parsed.values() if v["pass"])
    log.info("%sFilter: %d/%d items passed", prefix_tag, passed, total)
    return parsed, coverage or None


def _format_search_results(query: str, results: list[dict] | None) -> str:
    """Render a SERP into compact text for the conversation."""
    if not results:
        return f"Search '{query}': no results."
    lines = [f"Search '{query}':"]
    for r in results:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        url = (r.get("url") or "").strip()
        lines.append(f"- {title} — {snippet} ({url})")
    return "\n".join(lines)


async def curate_entries(
    items: list[dict],
    filter_cfg: dict,
    global_model: str | None = None,
    *,
    language: str = "EN-US",
    ledger: list[dict] | None = None,
    rollups: list[dict] | None = None,
    adapter: LLMAdapter,
    extra_instructions: str | None = None,
    reasoning: bool | str | dict = False,
    trace: dict | None = None,
    task_name: str | None = None,
) -> tuple[dict[str, dict], list[CoverageUpdate] | None] | None:
    """Filter feed entries through LLM and emit coverage updates.

    Returns (results, coverage) where results maps item ID → {"pass": bool, "reason": str}
    and coverage is the list of topics touched this run (or None if the LLM produced none).
    Returns None on failure (caller should fail-open: treat all entries as passing).
    `ledger`: the active coverage-ledger topics (most recent first) passed as context so the
    model can dedup, apply the escalating-trajectory bar, and continue topics across runs.
    """
    prefix_tag = f"[{task_name}] " if task_name else ""
    model, reasoning = _resolve_model_reasoning(filter_cfg, global_model, reasoning)
    explain = filter_cfg.get("explain", False)
    instructions = _build_curate_instructions(
        filter_cfg,
        language=language,
        ledger=ledger,
        rollups=rollups,
        extra_instructions=extra_instructions,
        explain=explain,
    )
    payload = json.dumps(items, ensure_ascii=False)
    if trace is not None:
        trace["instructions"] = instructions
        trace["payload"] = items

    total = sum(len(g.get("items", [])) for g in items)
    log.debug("Filtering %d entries with LLM (model=%s)", total, model)

    decisions = await adapter.complete_structured(
        payload,
        FilterDecisions,
        model=model,
        instructions=instructions,
        reasoning=reasoning,
        trace=trace,
    )
    if decisions is None:
        log.error("%sLLM filter returned no parseable response", prefix_tag)
        return None
    return _parse_decisions(decisions, prefix_tag, total, trace)


async def curate_entries_agentic(
    items: list[dict],
    filter_cfg: dict,
    global_model: str | None = None,
    *,
    language: str = "EN-US",
    ledger: list[dict] | None = None,
    rollups: list[dict] | None = None,
    adapter: LLMAdapter,
    extra_instructions: str | None = None,
    reasoning: bool | str | dict = False,
    trace: dict | None = None,
    task_name: str | None = None,
    corroborate_cfg: dict | None = None,
) -> tuple[dict[str, dict], list[CoverageUpdate] | None] | None:
    """Curate via a bounded agentic loop that corroborates with web search.

    One conversation seeded with [criteria+items] (a cache-stable prefix); each turn
    the LLM emits a CurateAction — `search` (queries fanned out concurrently to
    vascod) or `finish` — then the final verdict is produced over the same warm
    conversation. Action turns run without thinking (cheap); the final FilterDecisions
    uses the configured reasoning. Fail-open: a vascod `None` is treated as "no
    results"; an LLM `None` returns None so the caller fails open.
    """
    from process import _vasco

    prefix_tag = f"[{task_name}] " if task_name else ""
    model, reasoning = _resolve_model_reasoning(filter_cfg, global_model, reasoning)
    explain = filter_cfg.get("explain", False)
    judge_instructions = _build_curate_instructions(
        filter_cfg,
        language=language,
        ledger=ledger,
        rollups=rollups,
        extra_instructions=extra_instructions,
        explain=explain,
    )
    system = judge_instructions + "\n\n" + _SEARCH_PREAMBLE
    payload = json.dumps(items, ensure_ascii=False)
    research_nudge = (
        "RESEARCH PHASE — respond with a CurateAction ONLY (kind='search' with `queries`, "
        "or kind='finish'). Do NOT produce verdicts or the briefing yet; that comes after "
        "you finish."
    )
    convo: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Items to curate:\n{payload}\n\n{research_nudge}"},
    ]

    cfg = corroborate_cfg or {}
    max_steps = int(cfg.get("max_steps", 3))
    max_searches = int(cfg.get("max_searches", 8))
    max_results = int(cfg.get("max_results", 5))
    searched: set[str] = set()
    searches_done = 0
    steps_log: list[dict] = []

    for step in range(max_steps):
        action = await adapter.complete_structured(
            "", CurateAction, model=model, messages=convo, reasoning=False
        )
        if action is None:
            log.warning(
                "[%s] curate: action turn %d returned no parseable CurateAction — "
                "ending research, proceeding to verdict",
                task_name,
                step,
            )
            break
        step_rec = {
            "step": step,
            "kind": action.kind,
            "rationale": action.rationale,
            "queries": list(action.queries),
        }
        steps_log.append(step_rec)
        log.debug("[%s] curate step %d: %s — %s", task_name, step, action.kind, action.rationale)
        if action.kind == "finish":
            break
        todo = []
        for q in action.queries:
            q = (q or "").strip()
            if not q or q in searched or searches_done >= max_searches:
                continue
            searched.add(q)
            searches_done += 1
            todo.append(q)
        convo.append({"role": "assistant", "content": f"SEARCH: {', '.join(todo) or '(none)'}"})
        if todo:
            results = await asyncio.gather(
                *[_vasco.search(q, max_results=max_results) for q in todo]
            )
            step_rec["results"] = [
                {
                    "query": q,
                    "hits": [
                        {
                            "title": (r.get("title") or "")[:120],
                            "snippet": (r.get("snippet") or "")[:160],
                            "url": r.get("url") or "",
                        }
                        for r in (res or [])
                    ],
                }
                for q, res in zip(todo, results)
            ]
            blocks = [_format_search_results(q, r) for q, r in zip(todo, results)]
            convo.append({"role": "user", "content": "\n\n".join(blocks) + f"\n\n{research_nudge}"})
        else:
            convo.append(
                {"role": "user", "content": f"No new searches issued.\n\n{research_nudge}"}
            )
        if searches_done >= max_searches:
            break

    convo.append(
        {
            "role": "user",
            "content": "VERDICT PHASE — now produce the final FilterDecisions for ALL items: per-item verdicts and the memory briefing.",
        }
    )
    if trace is not None:
        trace["instructions"] = system
        trace["payload"] = items
        trace["steps"] = steps_log

    total = sum(len(g.get("items", [])) for g in items)
    log.debug("[%s] agentic curate: %d items, %d searches", task_name, total, searches_done)
    decisions = await adapter.complete_structured(
        "", FilterDecisions, model=model, messages=convo, reasoning=reasoning, trace=trace
    )
    if decisions is None:
        log.error("%sLLM agentic filter returned no parseable response", prefix_tag)
        return None
    return _parse_decisions(decisions, prefix_tag, total, trace)


def _decode_results(raw_results: dict[str, dict], id_map: dict[int, Item]) -> dict[str, dict]:
    """Map the LLM's int-id results to {item.id: {pass, reason}} using the int→Item map."""
    decoded: dict[str, dict] = {}
    for gid, v in raw_results.items():
        if gid.isdigit() and (item := id_map.get(int(gid))):
            decoded[item.id] = v
    return decoded


def _record_trace(
    collector,
    trace: dict,
    *,
    model: str | None,
    parsed: list[dict],
    memory: str | None,
) -> None:
    collector.record_filter(
        model=model,
        instructions=trace.get("instructions", ""),
        payload=trace.get("payload", []),
        raw_response=trace.get("raw_response"),
        parsed=parsed,
        memory=memory,
        model_used=trace.get("model_used"),
        input_tokens=trace.get("input_tokens"),
        output_tokens=trace.get("output_tokens"),
        latency_s=trace.get("latency_s"),
        reasoning=trace.get("reasoning"),
        cache_hit_tokens=trace.get("cache_hit_tokens"),
        cache_miss_tokens=trace.get("cache_miss_tokens"),
        steps=trace.get("steps"),
    )


async def curate_items(
    all_items: list[Item],
    curate_cfg: dict,
    handle: ModelHandle | None,
    *,
    language: str = "EN-US",
    ledger: list[dict] | None = None,
    rollups: list[dict] | None = None,
    collector=None,
    analysis: bool = False,
    task_name: str | None = None,
) -> CurateResult:
    """Run the LLM curate filter on items. Returns CurateResult with filter_pass set on each item.

    Items tagged `meta["curate_skip"]` bypass the LLM (they always pass). Fails
    open: a second LLM failure returns all items as passing.
    """
    # Build grouped payload for the LLM (grouped by source) with monotonically
    # increasing integer IDs across all sources.
    global_id = 0
    id_map: dict[int, Item] = {}
    payload_groups: list[dict] = []

    seen_sources: dict[str, list] = {}
    for item in all_items:
        seen_sources.setdefault(item.source, []).append(item)

    for source_name, items in seen_sources.items():
        group: dict = {"source": source_name, "items": []}
        for item in items:
            if item.meta.get("curate_skip"):
                continue
            payload_item: dict = {"id": global_id, "title": item.title, "url": item.url}
            desc = item.summary or item.body
            if desc:
                payload_item["description"] = desc
            group["items"].append(payload_item)
            id_map[global_id] = item
            global_id += 1
        if group["items"]:
            payload_groups.append(group)

    cite_map: dict[int, Citation] = {
        gid: Citation(item.source, item.url) for gid, item in id_map.items()
    }

    if not payload_groups:
        return CurateResult(items=all_items, coverage=None, cite_map=cite_map)

    if handle is None:
        log.error("LLM curate skipped — curate.model is not configured")
        return CurateResult(items=all_items, coverage=None, cite_map=cite_map)

    trace: dict | None = {} if collector else None
    effective_cfg = {**curate_cfg, "explain": True} if analysis else curate_cfg
    kwargs = dict(
        language=language,
        ledger=ledger,
        rollups=rollups,
        adapter=handle.adapter,
        extra_instructions=effective_cfg.get("instructions") or None,
        reasoning=handle.reasoning_for(analysis),
        trace=trace,
        task_name=task_name,
    )
    corroborate_cfg = curate_cfg.get("corroborate") or {}
    if corroborate_cfg.get("enabled"):

        async def _run():
            return await curate_entries_agentic(
                payload_groups,
                effective_cfg,
                handle.model,
                corroborate_cfg=corroborate_cfg,
                **kwargs,
            )
    else:

        async def _run():
            return await curate_entries(payload_groups, effective_cfg, handle.model, **kwargs)

    llm_return = await _run()
    if llm_return is None:
        log.warning("[%s] Filter failed, retrying in 10s", task_name or "?")
        await asyncio.sleep(10)
        llm_return = await _run()
    if llm_return is None:
        if collector and trace is not None:
            _record_trace(collector, trace, model=handle.model, parsed=[], memory=None)
        log.error("Filter failed twice — treating all items as passing")
        return CurateResult(items=all_items, coverage=None, cite_map=cite_map)

    raw_results, coverage = llm_return

    if collector and trace is not None:
        parsed_list = []
        for gid_str, v in raw_results.items():
            try:
                it = id_map.get(int(gid_str))
            except ValueError:
                it = None
            parsed_list.append(
                {
                    "id": gid_str,
                    "source": it.source if it else "?",
                    "title": it.title if it else "?",
                    "url": it.url if it else None,
                    "pass": v["pass"],
                    "reason": v["reason"],
                }
            )
        memory_for_trace = "\n\n".join(c.state for c in coverage) if coverage else None
        _record_trace(
            collector, trace, model=handle.model, parsed=parsed_list, memory=memory_for_trace
        )

    result_by_item_id = _decode_results(raw_results, id_map)

    annotated = [
        dc_replace(
            item,
            filter_pass=result_by_item_id[item.id]["pass"]
            if item.id in result_by_item_id
            else True,
            filter_reason=result_by_item_id[item.id]["reason"]
            if item.id in result_by_item_id
            else "",
        )
        for item in all_items
    ]

    return CurateResult(items=annotated, coverage=coverage, cite_map=cite_map)
