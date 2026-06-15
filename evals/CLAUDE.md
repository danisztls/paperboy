# evals/

Captured LLM-call traces.

## `capture.py`

`RunCapture` collects every LLM call's instructions, input, response, tokens, latency, optional reasoning. Exposes `to_jsonl_records()`, `write_jsonl()`, `to_json()`, and rich `display()`.

## JSONL record shape

`<state_dir>/evals/<task>/<run_iso>.jsonl`, one record per LLM call.

**Common keys:** `task`, `call_type` (`filter` | `summarize` | `research`), `ts`, `model_used`, `instructions`, `response`, `input_tokens`, `output_tokens`, `latency_s`, `reasoning`. (`research` records carry only the trajectory — no tokens/latency/cost.)

**`filter` adds:** `payload` (list of source groups with items, each item has `id`, `title`, `url`, optional `description`), `parsed` (per-item `id`, `source`, `title`, `url`, `pass`, `reason`), `memory`, `source_groups_count`, `items_count`, `passing_count`, `model` (configured spec), `steps` (the corroboration trajectory when `curate.corroborate` is on — per-step `kind`/`rationale`/`queries`, plus `results` on search steps: per query a list of `hits` of `{title, snippet, url}`, so the trace records the evidence the model actually saw; empty otherwise). Common keys `cache_hit_tokens`/`cache_miss_tokens` report prompt-cache prefix reuse (populated on the agentic path, where the `[criteria+items]` prefix is reused across turns). `--analysis --human` surfaces the trajectory (searched queries + top hits), the cache hit ratio, and the reasoning-trace size in the filter panel.

**`summarize` adds:** `input` (text sent to the LLM), `item_id`, `item_title`, `item_url`, `fetched_body`.

**`research` adds:** `prompt`, `model` (configured spec), `steps` (the agent trajectory: per-step `kind`/`rationale`/`queries`/`urls`), `sources` (gathered `url`/`title`).

## Caveats

- `reasoning` fires when either the per-spec `ModelSpec.reasoning` is set (`low|medium|high`) or `--analysis` is passed. The captured `reasoning` field is provider-dependent: populated when the provider returns a reasoning trace (Gemini thoughts, DeepSeek `reasoning_content`). Non-thinking models return `reasoning: null` regardless.
- Curate goes through `complete_structured`. DeepSeek honors reasoning there by delegating to `complete()` (forced JSON output conflicts with thinking) and the reasoning trace is carried into the capture; Gemini plumbs reasoning through structured output directly. Only DeepSeek + Gemini are supported.
- No rotation policy ships yet; clean up manually if disk pressure becomes an issue.
