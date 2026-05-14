import logging

from config import get_llm_pull_cfg
from llm.adapters.base import LLMAdapter
from pipeline import Item, PullResult, Source

log = logging.getLogger(__name__)


class LLMSearchSource(Source):
    """Pulls content from the web via an LLM web-search call."""

    def __init__(
        self,
        *,
        instructions: str | None = None,
        global_model: str | None = None,
        adapter: LLMAdapter,
    ) -> None:
        self._instructions = instructions
        self._global_model = global_model
        self._adapter = adapter

    async def pull(self, cfg: dict, seen: set[str], session) -> PullResult | None:
        text = await run_llm_task(
            cfg, self._instructions, self._global_model, adapter=self._adapter
        )
        if not text:
            return None
        name = cfg.get("name", "llm")
        item = Item(id=f"{name}:llm_result", title=name, source=name, body=text)
        return PullResult(new_items=[item], current_items=[])


async def run_llm_task(
    task_cfg: dict,
    instructions: str | None = None,
    global_model: str | None = None,
    *,
    adapter: LLMAdapter,
    reasoning: bool | dict = False,
    trace: dict | None = None,
) -> str | None:
    """Call LLM with web search. Returns response text or None on failure."""
    name = task_cfg.get("name")
    llm_cfg = get_llm_pull_cfg(task_cfg)
    model = llm_cfg.get("model") or global_model or None
    prompt = llm_cfg["prompt"]
    web_search = llm_cfg.get("web_search", True)
    task_instructions = llm_cfg.get("instructions")
    combined_instructions = "\n\n".join(filter(None, [instructions, task_instructions])) or None
    if trace is not None:
        trace["model"] = model
        trace["instructions"] = combined_instructions
        trace["prompt"] = prompt
        trace["web_search"] = bool(web_search)
    log.info("[%s] Calling LLM (model=%s): %s", name, model, prompt[:120])
    log.debug("[%s] Full prompt: %s", name, prompt)
    if combined_instructions:
        log.debug("[%s] Instructions: %s", name, combined_instructions[:200])
    resp = await adapter.complete(
        prompt,
        model=model,
        instructions=combined_instructions,
        web_search=web_search,
        reasoning=reasoning,
    )
    text = resp.text if resp else None
    if trace is not None and resp is not None:
        trace["raw_response"] = text
        trace["input_tokens"] = resp.input_tokens
        trace["output_tokens"] = resp.output_tokens
        trace["latency_s"] = resp.latency_s
        trace["model_used"] = resp.model
        if resp.reasoning:
            trace["reasoning"] = resp.reasoning
    if text:
        log.info("[%s] LLM response received (%d chars): %s", name, len(text), text[:120])
        log.debug("[%s] Full response:\n%s", name, text)
    else:
        log.warning("[%s] LLM returned empty response", name)
    return text
