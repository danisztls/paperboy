# evals/

Captured LLM-call traces and replay.

## `capture.py`

`RunCapture` collects every LLM call's instructions, input, response, tokens, latency, optional reasoning. Exposes `to_jsonl_records()`, `write_jsonl()`, `to_json()`, and rich `display()`.

## `replay.py`

Reads a JSONL produced by `RunCapture` and re-issues each captured call against a list of `provider:model` pairs (only the model varies; instructions/input are used verbatim). Writes a side-by-side JSON report to `<state_dir>/evals/replays/`.

## JSONL record shape

`<state_dir>/evals/<task>/<run_iso>.jsonl`, one record per LLM call.

**Common keys:** `task`, `call_type` (`filter` | `summarize` | `research`), `ts`, `model_used`, `instructions`, `response`, `input_tokens`, `output_tokens`, `latency_s`, `reasoning`, `web_search`. (`research` records carry only the trajectory — no tokens/latency/cost.)

**`filter` adds:** `payload` (list of source groups with items, each item has `id`, `title`, `url`, optional `description`), `parsed` (per-item `id`, `source`, `title`, `url`, `pass`, `reason`, `axes` = the 0-3 `{magnitude, dissonance, credibility, redundancy, relevance}` scores, plus `llm_pass` only when a `decision.mode: scored` rule overrode the LLM's verdict), `memory`, `source_groups_count`, `items_count`, `passing_count`, `model` (configured spec).

**`summarize` adds:** `input` (text sent to the LLM), `item_id`, `item_title`, `item_url`, `fetched_body`.

**`research` adds:** `prompt`, `model` (configured spec), `steps` (the agent trajectory: per-step `kind`/`rationale`/`queries`/`urls`), `sources` (gathered `url`/`title`).

## Replay output shape

`<state_dir>/evals/replays/<source_basename>__replay_<ts>.json`:

```
{source_run, replayed_at, models, call_filter, calls: [{call_type, task, ts, web_search, item_id?, item_title?, results: [{model, text, input_tokens, output_tokens, latency_s, reasoning, is_original?, error?}, ...]}]}
```

The first entry in `results` is the original captured response (`is_original: true`); subsequent entries are one per replayed `provider:model`.

## Caveats

- Replays of calls with `web_search: true` (configurable for `filter`) are noisy because each run gets different search results.
- Replay uses captured `instructions` + `input` verbatim — only the model varies. Prompt-change comparisons require capturing a new run after the change.
- `reasoning` fires when either the per-spec `ModelSpec.reasoning` is set (`low|medium|high`) or `--analysis` is passed. The captured `reasoning` field is provider-dependent: populated when the provider returns a reasoning trace (Gemini thoughts, DeepSeek `reasoning_content`). Non-thinking models return `reasoning: null` regardless.
- Curate (which goes through `complete_structured`) is supported on OpenAI + Gemini; Anthropic + DeepSeek ignore reasoning on structured calls (one-time warning) because their forced-structured-output paths conflict with thinking.
- No rotation policy ships yet; clean up manually if disk pressure becomes an issue.
