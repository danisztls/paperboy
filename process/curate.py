import json
import logging
import textwrap

from pydantic import BaseModel

from providers.llm.base import LLMAdapter

log = logging.getLogger(__name__)


class FilterItem(BaseModel):
    """One item's filter verdict. `passes` is the JSON field `pass` for LLM compatibility."""

    id: int
    passes: bool
    reason: str


class FilterDecisions(BaseModel):
    """Structured-output shape produced by the LLM curate call."""

    items: list[FilterItem]
    memory: str = ""


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
) -> tuple[dict[str, dict], str | None] | None:
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
            "Previous memory log (oldest → newest — your evolving view of this information space):\n"
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
        - Fail any item that covers a story already present in the memory log above without adding a significant new development. Use reason: 'already covered'.
        - Within this batch, if multiple items cover the same event, keep only the one(s) that contribute the most relevant information; fail the rest with reason: 'duplicate within batch'.

        **Step 3 — Write memory.** Write a factual news briefing in {language} covering every item that passed the filter. Rules:
        - Include ALL passing items — do not omit any.
        - One story per paragraph (blank line between stories). Write 2–3 sentences per story: the first sentence states the core fact; subsequent sentences add context, significance, key figures, numbers, or consequences. Never mix two different topics in one paragraph.
        - Never use semicolons to chain unrelated events in the same sentence.
        - Group by theme; within each group order by significance. Lead with the single most significant development overall.
        - Append the story's numeric id in square brackets after the period of the last sentence only, e.g. 'Talks broke down over tariffs [3].' — use the integer id values from the input.
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
    memory_text = decisions.memory.strip() or None
    passed = sum(1 for v in parsed.values() if v["pass"])
    log.info("Filter: %d/%d items passed", passed, total)
    return parsed, memory_text
