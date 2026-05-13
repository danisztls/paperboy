import json
import logging

from llm.adapters.base import LLMAdapter

log = logging.getLogger(__name__)


async def filter_entries(
    items: list[dict],
    filter_cfg: dict,
    global_model: str | None = None,
    *,
    language: str = "EN-US",
    memory_history: list[tuple[str, str]] | None = None,
    adapter: LLMAdapter,
    extra_instructions: str | None = None,
    capture: dict | None = None,
) -> tuple[dict[str, dict], str | None] | None:
    """Filter feed entries through LLM and optionally update memory.

    Returns (results, memory_text) where results maps item ID → {"pass": bool, "reason": str}
    and memory_text is the new memory entry (or None if the LLM didn't produce one).
    Returns None on failure (caller should fail-open: treat all entries as passing).
    memory_history: chronological list of prior memory entries (oldest first) passed as
    context so the model can build continuity across runs.
    """
    model = filter_cfg.get("model") or global_model or None
    explain = filter_cfg.get("explain", False)
    criteria = filter_cfg.get("prompt", "")
    memory_block = ""
    if memory_history:
        entries = "\n---\n".join(f"[{ts[:16]}] {text}" for ts, text in memory_history)
        memory_block = (
            "Previous memory log (oldest → newest — your evolving view of this information space):\n"
            + entries
            + "\n\n"
        )
    instructions = (
        f"## Filter criteria\n{criteria}\n\n"
        + (f"## Additional instructions\n{extra_instructions}\n\n" if extra_instructions else "")
        + f"{memory_block}"
        "## Input\n\n"
        "You will receive a JSON array of source groups, each with a 'source' name and an 'items' array. "
        "Each item has an integer 'id', a 'title', and an optional 'description'.\n\n"
        "## Steps\n\n"
        "**Step 1 — Filter.** For each item, decide whether it matches the filter criteria above. "
        "Mark it pass: true if it does, pass: false otherwise.\n\n"
        "**Step 2 — Deduplicate.**\n"
        "- Fail any item that covers a story already present in the memory log above without adding a significant new development. Use reason: 'already covered'.\n"
        "- Within this batch, if multiple items cover the same event, keep only the one(s) that contribute the most relevant information; fail the rest with reason: 'duplicate within batch'.\n\n"
        f"**Step 3 — Write memory.** Write a factual news briefing in {language} covering every item that passed the filter. Rules:\n"
        "- Include ALL passing items — do not omit any.\n"
        "- One story per line (newline-separated). Never use semicolons to chain unrelated stories on the same line. Never mix two different topics in one sentence.\n"
        "- Group by theme; within each group order by significance. Lead with the single most significant development overall.\n"
        "- After each sentence, append the story's numeric id in square brackets immediately after the period, e.g. 'Trump signed deal [3].' — use the integer id values from the input.\n"
        "- No meta-commentary about the filtering process, no mention of what was discarded, no hedging phrases.\n"
        "- Include enough factual specificity that a follow-up story can be recognised as a continuation on the next run.\n\n"
        "## Output format\n\n"
        'Return a JSON object with exactly two top-level keys:\n'
        '- "items": array where each element is {"id": <integer>, "pass": true/false, "reason": "<explanation>"}. Include ALL input items, both passing and failing.\n'
        '- "memory": string — the news briefing from Step 3.\n\n'
        + (
            'Reason format — PASSING items: 2–3 sentence plain-language explanation of what the story is about and why it is relevant, written for someone with no prior context (ELI5 style).\n'
            'Reason format — FAILING items: one short sentence explaining why it was excluded.\n'
            if explain else
            'Keep the "reason" field to one short sentence for all items.\n'
        )
        + "\nReturn ONLY a valid JSON object, no other text."
    )
    if capture is not None:
        capture["instructions"] = instructions
        capture["payload"] = items

    payload = json.dumps(items, ensure_ascii=False)
    total = sum(len(g.get("items", [])) for g in items)
    log.info("Filtering %d entries with LLM (model=%s)", total, model)
    log.debug("Filter criteria: %s", criteria)
    web_search = filter_cfg.get("web_search")

    text = await adapter.complete(
        payload,
        model=model,
        instructions=instructions,
        web_search=web_search or False,
    )
    if capture is not None:
        capture["raw_response"] = text
    if not text:
        log.error("LLM filter returned empty response")
        return None

    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    log.debug("Filter LLM response: %s", text[:500])
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("LLM filter returned invalid JSON: %s — %s", exc, text[:200])
        return None

    if isinstance(result, list):
        log.warning("LLM filter returned bare array — memory will not be saved; model may be ignoring format instruction")
        items_list = result
        memory_text = None
    elif isinstance(result, dict) and "items" in result:
        items_list = result["items"]
        memory_text = str(result.get("memory", "")).strip() or None
    else:
        log.warning("LLM filter returned unexpected format: %s", text[:200])
        return None

    parsed = {str(r["id"]): {"pass": bool(r.get("pass")), "reason": str(r.get("reason", ""))} for r in items_list if "id" in r}
    passed = sum(1 for v in parsed.values() if v["pass"])
    log.info("Filter: %d/%d items passed", passed, total)
    return parsed, memory_text
