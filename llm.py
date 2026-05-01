import logging

from openai import AsyncOpenAI

DEFAULT_MODEL = "gpt-5.4-mini"

log = logging.getLogger(__name__)


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
