"""LLM summarization for articles and YouTube transcripts."""

import asyncio
import logging
import sys
from dataclasses import replace as dc_replace

from pipeline import Item
from process._vasco import fetch_content, fetch_content_with_title
from providers.llm.base import LLMAdapter, ModelHandle

log = logging.getLogger(__name__)


async def summarize_entry(
    title: str,
    description: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    language: str | None = None,
    instructions: str | None = None,
    reasoning: bool | str | dict = False,
    trace: dict | None = None,
) -> str | None:
    lang_directive = f"Write in {language}. " if language else ""
    base_instructions = (
        f"You are a precise, concise summarizer. {lang_directive}"
        "Given the title and description of a news article or feed entry, write a brief summary "
        "covering the main point and key details. No filler phrases. "
        "Keep the summary under 1024 characters."
    )
    combined = f"{base_instructions} {instructions}" if instructions else base_instructions
    input_text = f"Title: {title}\n\nDescription:\n{description}"
    if trace is not None:
        trace["instructions"] = combined
        trace["input"] = input_text
    log.debug("Summarizing entry: %s", title[:80])
    resp = await adapter.complete(
        input_text, model=model, instructions=combined, reasoning=reasoning
    )
    if resp is None:
        if trace is not None:
            trace["output"] = None
        return None
    log.debug("Summarized in %.1fs with %s: %s", resp.latency_s, resp.model, title[:60])
    if trace is not None:
        trace["output"] = resp.text
        trace["input_tokens"] = resp.input_tokens
        trace["output_tokens"] = resp.output_tokens
        trace["latency_s"] = resp.latency_s
        trace["model_used"] = resp.model
        if resp.reasoning:
            trace["reasoning"] = resp.reasoning
    return resp.text


async def summarize_items(
    items: list[Item],
    cfg_by_id: dict[str, tuple[str | None, str | None]],
    handle: ModelHandle | None,
    *,
    collector=None,
    analysis: bool = False,
) -> list[Item]:
    """Replace .summary on items that have fetchable content or a body, concurrently.

    `cfg_by_id` maps item id → (language, instructions); language None lets the
    LLM mirror the content's language. Also fills Item.image with the article's
    og:image when the item had no image yet, piggybacking on the content fetch
    vasco already performed.
    """

    async def _get_content(e: Item) -> tuple[str, str | None]:
        fetched = await fetch_content(e.url)
        if fetched:
            return fetched
        return e.body, None

    if handle is None:
        log.error("Summarize skipped — summarize.model is not configured")
        return items

    async def _fetch_and_summarize(
        e: Item,
    ) -> tuple[Item, str | None, str | None, str | None, dict | None]:
        content, og_image = await _get_content(e)
        if not content:
            return e, None, None, None, None
        trace: dict | None = {} if collector else None
        try:
            summary = await summarize_entry(
                e.title,
                content,
                handle.adapter,
                model=handle.model,
                language=cfg_by_id[e.id][0],
                instructions=cfg_by_id[e.id][1],
                reasoning=handle.reasoning_for(analysis),
                trace=trace,
            )
        except Exception as exc:
            log.error("summarize_entry failed for %s: %s", e.url, exc)
            summary = None
        return e, content, summary, og_image, trace

    def _dedup_key(e: Item) -> str:
        return e.url or f"__no_url__:{e.id}"

    seen_keys: set[str] = set()
    unique: list[Item] = []
    for e in items:
        key = _dedup_key(e)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(e)

    results = await asyncio.gather(*[_fetch_and_summarize(e) for e in unique])

    by_key: dict[str, tuple[str | None, str | None]] = {}
    for e, content, summary, og_image, trace in results:
        by_key[_dedup_key(e)] = (summary, og_image)
        if collector and trace is not None:
            collector.record_summarization(
                item_id=e.id,
                title=e.title,
                url=e.url,
                fetched_body=content,
                instructions=trace.get("instructions", ""),
                input_text=trace.get("input", ""),
                summary=summary,
                model_used=trace.get("model_used"),
                input_tokens=trace.get("input_tokens"),
                output_tokens=trace.get("output_tokens"),
                latency_s=trace.get("latency_s"),
                reasoning=trace.get("reasoning"),
            )

    updated: dict[str, Item] = {}
    for e in items:
        summary, og_image = by_key.get(_dedup_key(e), (None, None))
        fields: dict = {}
        if summary:
            fields["summary"] = summary
        if og_image and not e.image:
            fields["image"] = og_image
        if fields:
            updated[e.id] = dc_replace(e, **fields)
    return [updated.get(e.id, e) for e in items]


async def summarize_transcript(
    title: str,
    transcript: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    language: str | None = None,
) -> str | None:
    lang_directive = f"Write in {language}. " if language else ""
    instructions = (
        f"You are a precise, concise summarizer. {lang_directive}"
        "Given the title and transcript of a YouTube video, write a clear summary covering the main topics and key takeaways. "
        "Use a few short paragraphs. No filler phrases or meta-commentary about the summarization process."
    )
    log.info(
        "Summarizing transcript (model=%s, language=%s, %d chars)", model, language, len(transcript)
    )
    resp = await adapter.complete(
        f"Title: {title}\n\nTranscript:\n{transcript[:12000]}",
        model=model,
        instructions=instructions,
    )
    return resp.text if resp else None


async def run_summarize(
    url: str, adapter: LLMAdapter, model: str | None, language: str = "EN-US"
) -> None:
    result = await fetch_content_with_title(url, refresh=True)
    if not result:
        log.error("Could not fetch content for %s", url)
        sys.exit(1)

    content, title, _image, is_youtube = result
    if is_youtube:
        summary = await summarize_transcript(
            title, content, adapter, model=model, language=language
        )
    else:
        summary = await summarize_entry(
            title or url, content, adapter, model=model, language=language
        )

    if summary:
        print(summary)
    else:
        log.error("Summarization failed")
        sys.exit(1)


async def run_get_content(url: str) -> None:
    result = await fetch_content_with_title(url, refresh=True)
    if not result:
        log.error("Could not fetch content for %s", url)
        sys.exit(1)
    content, _title, _image, _is_youtube = result
    print(content)
