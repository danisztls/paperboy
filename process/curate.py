import asyncio
import json
import logging
import textwrap
from dataclasses import replace as dc_replace

from pydantic import BaseModel

from pipeline import Citation, CurateResult, Item, MemoryParagraph
from providers.llm.base import LLMAdapter, ModelHandle

log = logging.getLogger(__name__)


class FilterItem(BaseModel):
    """One item's filter verdict. `passes` is the JSON field `pass` for LLM compatibility."""

    id: int
    passes: bool
    reason: str


class CurateParagraph(BaseModel):
    """One paragraph of the memory briefing with its supporting item IDs."""

    section: str | None = None
    text: str
    citations: list[int] = []


class FilterDecisions(BaseModel):
    """Structured-output shape produced by the LLM curate call."""

    items: list[FilterItem]
    memory: list[CurateParagraph] = []


async def curate_entries(
    items: list[dict],
    filter_cfg: dict,
    global_model: str | None = None,
    *,
    language: str = "EN-US",
    memory_history: list[tuple[str, str]] | None = None,
    adapter: LLMAdapter,
    extra_instructions: str | None = None,
    reasoning: bool | str | dict = False,
    trace: dict | None = None,
    task_name: str | None = None,
) -> tuple[dict[str, dict], list[MemoryParagraph] | None] | None:
    """Filter feed entries through LLM and optionally update memory.

    Returns (results, memory_text) where results maps item ID → {"pass": bool, "reason": str}
    and memory_text is the new memory entry (or None if the LLM didn't produce one).
    Returns None on failure (caller should fail-open: treat all entries as passing).
    memory_history: chronological list of prior memory entries (oldest first) passed as
    context so the model can build continuity across runs.
    """
    prefix_tag = f"[{task_name}] " if task_name else ""
    raw_model = filter_cfg.get("model")
    model = (raw_model.get("name") if isinstance(raw_model, dict) else None) or global_model or None
    if not reasoning and isinstance(raw_model, dict) and raw_model.get("reasoning"):
        reasoning = raw_model["reasoning"]
    explain = filter_cfg.get("explain", False)
    criteria = filter_cfg.get("criteria", "")

    prefix = f"## Filter criteria\n{criteria}\n\n"
    if extra_instructions:
        prefix += f"## Additional instructions\n{extra_instructions}\n\n"
    if memory_history:
        entries = "\n---\n".join(f"[{ts[:16]}] {text}" for ts, text in memory_history)
        prefix += (
            "## Already-published digests (DO NOT re-report)\n"
            "The following are summaries of content that was already sent to readers in previous digest runs (oldest → newest). "
            "Use them ONLY to identify stories that have already been covered. "
            "They are not sources to cite — never reference them with a citation marker or attribute a fact to them.\n\n"
            + entries
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

        **Step 2 — Deduplicate.**
        - Compare each item against the already-published digests above. Fail any item whose core story was already sent to readers and that does not introduce a significant new development (new facts, updated numbers, meaningful consequence). Use reason: 'already covered'. The goal is that readers never see the same story twice without a genuine update.
        - Escalating-trajectory bar: the more times a trajectory has ALREADY appeared across the digests above, the higher the bar for sending another instalment. For a trajectory seen once, a concrete update may pass; for one covered repeatedly, mere incremental movement — another number, another routine step, another day of the same trend — is 'more of the same' and should fail with reason: 'trajectory already covered'. Pass it only when the new development changes the reader's picture: a reversal, a resolution, a turning point, a newly-realised consequence, or a structural rupture. Judge the trajectory by what the reader already knows from prior digests, not by whether any single number moved.
        - Within this batch, if multiple items cover the same event, keep only the one(s) that contribute the most relevant information; fail the rest with reason: 'duplicate within batch'.

        **Step 3 — Write memory.** Populate the `memory` array with a factual news briefing in {language}, one object per story. Each object has:
        - `section`: short thematic heading (e.g. "Brasil", "Geopolítica", "Economia"). Set ONLY on the first paragraph of a new thematic group; use `null` for all subsequent paragraphs in the same group. Never put section names inside `text`.
        - `text`: 1–3 sentences of plain prose. Lead with the core fact; add only the most essential detail (key figure, number, date, place, or consequence). No citation markers, brackets, section names, or meta-commentary in the text.
        - `citations`: list of integer IDs from this batch whose content directly supports the paragraph. Usually one ID; use multiple only when two items genuinely cover the same event. Never reference IDs from the already-published digests.

        Rules for the briefing as a whole:
        - Include ALL passing items — do not omit any.
        - One object per story; never mix two distinct topics in one object. Do not pad.
        - Never use semicolons to chain unrelated events in the same sentence.
        - Group by theme using the `section` field; within each group order by significance. Lead with the single most significant development overall.
        - No meta-commentary about the filtering process, no mention of what was discarded, no hedging phrases.
        - Include enough factual specificity (names, numbers, dates, places) that a follow-up story on the same event can be recognised as a continuation on the next run.

        ## Output

        Include ALL input items in `items`, both passing and failing. Populate `memory` with the news briefing from Step 3.

        {reason_format}
    """)

    instructions = prefix + body
    payload = json.dumps(items, ensure_ascii=False)
    if trace is not None:
        trace["instructions"] = instructions
        trace["payload"] = items

    total = sum(len(g.get("items", [])) for g in items)
    log.debug("Filtering %d entries with LLM (model=%s)", total, model)
    log.debug("Filter criteria: %s", criteria)

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

    if trace is not None:
        trace["raw_response"] = decisions.model_dump_json()

    parsed = {
        str(item.id): {"pass": item.passes, "reason": item.reason} for item in decisions.items
    }
    paragraphs = [
        MemoryParagraph(text=p.text, citations=p.citations, section=p.section)
        for p in decisions.memory
    ]
    passed = sum(1 for v in parsed.values() if v["pass"])
    log.info("%sFilter: %d/%d items passed", prefix_tag, passed, total)
    return parsed, paragraphs or None


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
    )


async def curate_items(
    all_items: list[Item],
    curate_cfg: dict,
    handle: ModelHandle | None,
    *,
    language: str = "EN-US",
    memory_history: list[tuple[str, str]] | None = None,
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
        return CurateResult(items=all_items, memory=None, cite_map=cite_map)

    if handle is None:
        log.error("LLM curate skipped — curate.model is not configured")
        return CurateResult(items=all_items, memory=None, cite_map=cite_map)

    trace: dict | None = {} if collector else None
    effective_cfg = {**curate_cfg, "explain": True} if analysis else curate_cfg
    kwargs = dict(
        language=language,
        memory_history=memory_history,
        adapter=handle.adapter,
        extra_instructions=effective_cfg.get("instructions") or None,
        reasoning=handle.reasoning_for(analysis),
        trace=trace,
        task_name=task_name,
    )
    llm_return = await curate_entries(payload_groups, effective_cfg, handle.model, **kwargs)
    if llm_return is None:
        log.warning("[%s] Filter failed, retrying in 10s", task_name or "?")
        await asyncio.sleep(10)
        llm_return = await curate_entries(payload_groups, effective_cfg, handle.model, **kwargs)
    if llm_return is None:
        if collector and trace is not None:
            _record_trace(collector, trace, model=handle.model, parsed=[], memory=None)
        log.error("Filter failed twice — treating all items as passing")
        return CurateResult(items=all_items, memory=None, cite_map=cite_map)

    raw_results, memory_paragraphs = llm_return

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
        memory_for_trace = (
            "\n\n".join(p.text for p in memory_paragraphs) if memory_paragraphs else None
        )
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

    return CurateResult(items=annotated, memory=memory_paragraphs, cite_map=cite_map)
