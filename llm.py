import logging

from openai import AsyncOpenAI

DEFAULT_MODEL = "gpt-5.4-mini"

log = logging.getLogger(__name__)


async def run_llm_task(task_cfg: dict, instructions: str | None = None, global_model: str | None = None) -> str | None:
    """Call OpenAI Responses API with web_search_preview. Returns response text or None on failure."""
    client = AsyncOpenAI()
    model = task_cfg.get("model") or global_model or DEFAULT_MODEL
    prompt = task_cfg["prompt"]
    tool = {"type": "web_search_preview", **task_cfg.get("tools", {})}
    try:
        response = await client.responses.create(
            model=model,
            tools=[tool],
            input=prompt,
            **({"instructions": instructions} if instructions else {}),
        )
        return response.output_text or None
    except Exception as exc:
        log.error("LLM task '%s' failed: %s", task_cfg.get("name"), exc)
        return None
