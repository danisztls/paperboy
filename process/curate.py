import json
import logging
import textwrap

from pydantic import BaseModel

from pipeline import MemoryParagraph
from providers.llm.base import LLMAdapter

log = logging.getLogger(__name__)


class FilterItem(BaseModel):
    """One item's filter verdict. `passes` is the JSON field `pass` for LLM compatibility."""

    id: int
    passes: bool
    reason: str


class CurateParagraph(BaseModel):
    """One paragraph of the memory briefing with its supporting item IDs."""

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
    reasoning: bool | dict = False,
    trace: dict | None = None,
) -> tuple[dict[str, dict], list[MemoryParagraph] | None] | None:
    """Filter feed entries through LLM and optionally update memory.

    Returns (results, memory_text) where results maps item ID → {"pass": bool, "reason": str}
    and memory_text is the new memory entry (or None if the LLM didn't produce one).
    Returns None on failure (caller should fail-open: treat all entries as passing).
    memory_history: chronological list of prior memory entries (oldest first) passed as
    context so the model can build continuity across runs.
    """
    raw_model = filter_cfg.get("model")
    model = (
        (next(iter(raw_model.values())) if isinstance(raw_model, dict) else raw_model)
        or global_model
        or None
    )
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
        - Within this batch, if multiple items cover the same event, keep only the one(s) that contribute the most relevant information; fail the rest with reason: 'duplicate within batch'.

        **Step 3 — Write memory.** Populate the `memory` array with a factual news briefing in {language}, one object per story. Each object has:
        - `text`: 1–3 sentences of plain prose. Lead with the core fact; add only the most essential detail (key figure, number, date, place, or consequence). No citation markers, brackets, or meta-commentary in the text.
        - `citations`: list of integer IDs from this batch whose content directly supports the paragraph. Usually one ID; use multiple only when two items genuinely cover the same event. Never reference IDs from the already-published digests.

        Rules for the briefing as a whole:
        - Include ALL passing items — do not omit any.
        - One object per story; never mix two distinct topics in one object. Do not pad.
        - Never use semicolons to chain unrelated events in the same sentence.
        - Group by theme; within each group order by significance. Lead with the single most significant development overall.
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
        trace["web_search"] = bool(filter_cfg.get("web_search"))

    total = sum(len(g.get("items", [])) for g in items)
    log.info("Filtering %d entries with LLM (model=%s)", total, model)
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
        log.error("LLM filter returned no parseable response")
        return None

    if trace is not None:
        trace["raw_response"] = decisions.model_dump_json()

    parsed = {
        str(item.id): {"pass": item.passes, "reason": item.reason} for item in decisions.items
    }
    paragraphs = [MemoryParagraph(text=p.text, citations=p.citations) for p in decisions.memory]
    passed = sum(1 for v in parsed.values() if v["pass"])
    log.info("Filter: %d/%d items passed", passed, total)
    return parsed, paragraphs or None
