"""LLM summarization for articles and YouTube transcripts."""

import logging
import sys

from process._vasco import fetch_content_with_title
from providers.llm.base import LLMAdapter

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
