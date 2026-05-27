# providers/llm/

LLM adapter implementations and the `ModelSpec` capability registry.

## Provider docs

When in doubt about an adapter's API or model behavior:

- OpenAI: https://developers.openai.com/api/docs
- Anthropic: https://platform.claude.com/docs/en/home
- Gemini: https://ai.google.dev/
- DeepSeek: https://api-docs.deepseek.com/

## Files

- `__init__.py` — `get_adapter(provider, api_key)` factory; returns `OpenAIAdapter`, `GeminiAdapter`, `AnthropicAdapter`, or `DeepSeekAdapter`. Also exports `FallbackAdapter`, which takes `list[tuple[adapter, model, default_reasoning]]` and tries entries in order until one returns non-None. Each entry carries its own default reasoning level; a caller's truthy `reasoning` arg overrides every entry (this is how `--analysis` forces reasoning on), while falsy/None lets each entry use its own default.
- `base.py` — `LLMAdapter` ABC with two abstract methods: `complete(...) -> LLMResponse | None` for free-form text and `complete_structured(prompt, response_model, ...) -> BaseModel | None` for provider-native structured output. Plus the `LLMResponse` dataclass (`text`, `model`, `input_tokens`, `output_tokens`, `latency_s`, `reasoning`, `finish_reason`). Both methods accept `reasoning: bool | str | dict = False` — strings `"off"|"low"|"medium"|"high"` are the canonical form (set via `ModelSpec.reasoning`); booleans and dicts are accepted for back-compat and per-call overrides. The shared helper `reasoning_level(value)` returns the level string (or `None` for off); each adapter maps the level to its provider-specific shape.
- `models.json` — capability registry consumed by `config/__init__.py`'s `ModelSpec` validator. Maps `provider → {model_name → {thinking: bool, web_search: bool, deprecated?: bool}}`. Unknown model names produce a validate-time warning; `reasoning: low|medium|high` on a model with `thinking: false` is a hard error; `deprecated: true` entries still validate but emit a one-line warning so the user knows to migrate. Add new releases here so they validate cleanly.

## Adapters

- `openai.py` — Responses API. Supports `web_search_preview` and `reasoning={"effort": <level>, "summary": "auto"}`. Structured output via `responses.parse(text_format=..., reasoning=...)` — reasoning is plumbed through `complete_structured()`.
- `gemini.py` — `google-genai`. Supports Google Search tool and `ThinkingConfig(include_thoughts=True, thinking_budget=<per-level>)`. Structured output via `GenerateContentConfig(response_mime_type="application/json", response_schema=..., thinking_config=...)` — reasoning is plumbed through `complete_structured()`.
- `anthropic.py` — streaming Messages API. Supports `web_search` tool and `thinking={"type": "enabled", "budget_tokens": <per-level>}`. **`complete_structured()` uses forced `tool_choice={"type": "tool", ...}` which is incompatible with extended thinking — reasoning is ignored on structured calls (a one-time `log.warning` is emitted).**
- `deepseek.py` — OpenAI-compatible Chat Completions. `complete()` toggles thinking per-request via `extra_body={"thinking": {"type": "enabled"/"disabled"}}` based on the resolved reasoning level. `complete_structured()` branches on reasoning: when truthy, delegates to `complete()` (which supports thinking) and parses the free-form response as JSON via Pydantic; when falsy, uses `response_format={"type": "json_object"}` with thinking disabled (the API rejects combining thinking with strict JSON).
