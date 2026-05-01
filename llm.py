import json
import logging

from openai import AsyncOpenAI

DEFAULT_MODEL = "gpt-5.4-mini"

log = logging.getLogger(__name__)


async def filter_entries(
    items: list[dict],
    filter_cfg: dict,
    global_model: str | None = None,
) -> dict[str, dict] | None:
    """Filter feed entries through LLM.

    Returns a dict mapping item ID → {"pass": bool, "reason": str}, or None on failure
    (caller should fail-open: treat all entries as passing).
    """
    client = AsyncOpenAI()
    model = filter_cfg.get("model") or global_model or DEFAULT_MODEL
    criteria = filter_cfg.get("prompt", "")
    instructions = (
        f"{criteria}\n\n"
        "You will receive a JSON array of feed items (each with id, title, description). "
        "For each item, decide if it matches the criteria above. "
        'Return a JSON array where each element is {"id": "<item id>", "pass": true/false, "reason": "<one short sentence>"}. '
        "Include ALL input items in the output, both passing and failing. "
        "Return ONLY a valid JSON array, no other text."
    )
    payload = json.dumps(items, ensure_ascii=False)
    log.info("Filtering %d entries with LLM (model=%s)", len(items), model)
    log.debug("Filter criteria: %s", criteria)
    try:
        response = await client.responses.create(
            model=model,
            instructions=instructions,
            input=payload,
        )
        text = (response.output_text or "").strip()
        log.debug("Filter LLM response: %s", text[:500])
        result = json.loads(text)
        if not isinstance(result, list):
            log.warning("LLM filter returned non-list response: %s", text[:200])
            return None
        parsed = {str(r["id"]): {"pass": bool(r.get("pass")), "reason": str(r.get("reason", ""))} for r in result if "id" in r}
        passed = sum(1 for v in parsed.values() if v["pass"])
        log.info("Filter: %d/%d items passed", passed, len(items))
        return parsed
    except Exception as exc:
        log.error("LLM filter failed: %s", exc)
        return None


async def run_llm_task(task_cfg: dict, instructions: str | None = None, global_model: str | None = None) -> str | None:
    """Call OpenAI Responses API with web_search_preview. Returns response text or None on failure."""
    client = AsyncOpenAI()
    name = task_cfg.get("name")
    model = task_cfg.get("model") or global_model or DEFAULT_MODEL
    prompt = task_cfg["prompt"]
    tool = {"type": "web_search_preview", **task_cfg.get("tools", {})}
    log.info("[%s] Calling LLM (model=%s): %s", name, model, prompt[:120])
    log.debug("[%s] Full prompt: %s", name, prompt)
    if instructions:
        log.debug("[%s] Instructions: %s", name, instructions[:200])
    try:
        response = await client.responses.create(
            model=model,
            tools=[tool],
            input=prompt,
            **({"instructions": instructions} if instructions else {}),
        )
        text = response.output_text or None
        if text:
            log.info("[%s] LLM response received (%d chars): %s", name, len(text), text[:120])
            log.debug("[%s] Full response:\n%s", name, text)
        else:
            log.warning("[%s] LLM returned empty response", name)
        return text
    except Exception as exc:
        log.error("LLM task '%s' failed: %s", name, exc)
        return None
