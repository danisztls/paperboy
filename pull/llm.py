import logging

from openai import AsyncOpenAI

from config import _get_llm_pull_cfg
from pipeline import Item, PullResult, Source

DEFAULT_MODEL = "gpt-5.4-mini"

log = logging.getLogger(__name__)


class LLMSearchSource(Source):
    """Pulls content from the web via an LLM web-search call."""

    def __init__(
        self,
        *,
        instructions: str | None = None,
        global_model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._instructions = instructions
        self._global_model = global_model
        self._api_key = api_key

    async def pull(self, cfg: dict, seen: set[str], session) -> PullResult | None:
        text = await run_llm_task(cfg, self._instructions, self._global_model, api_key=self._api_key)
        if not text:
            return None
        name = cfg.get("name", "llm")
        item = Item(id=f"{name}:llm_result", title=name, source=name, body=text)
        return PullResult(new_items=[item], current_items=[])


async def run_llm_task(task_cfg: dict, instructions: str | None = None, global_model: str | None = None, *, api_key: str | None = None) -> str | None:
    """Call OpenAI Responses API with web_search_preview. Returns response text or None on failure."""
    client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
    name = task_cfg.get("name")
    llm_cfg = _get_llm_pull_cfg(task_cfg)
    model = llm_cfg.get("model") or global_model or DEFAULT_MODEL
    prompt = llm_cfg["prompt"]
    web_search = llm_cfg.get("web_search", {})
    tool: dict = {"type": "web_search_preview"}
    if isinstance(web_search, dict):
        tool.update(web_search)
    task_instructions = llm_cfg.get("instructions")
    combined_instructions = "\n\n".join(filter(None, [instructions, task_instructions])) or None
    log.info("[%s] Calling LLM (model=%s): %s", name, model, prompt[:120])
    log.debug("[%s] Full prompt: %s", name, prompt)
    if combined_instructions:
        log.debug("[%s] Instructions: %s", name, combined_instructions[:200])
    try:
        response = await client.responses.create(
            model=model,
            tools=[tool],
            input=prompt,
            **({"instructions": combined_instructions} if combined_instructions else {}),
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
