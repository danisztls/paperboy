# benchmark/

Standalone script that runs a fixed set of URLs through multiple LLM providers and compares their summaries.

## Files

- `__main__.py` — entry point; run with `uv run benchmark/`
- `config.yaml` — active config (not committed); copy from `config.yaml.template`
- `config.yaml.template` — documents `urls` (list of YouTube or article URLs) and `models` (list of `{provider, model, label}` dicts)
- `results/` — JSON output files (`benchmark_<timestamp>.json`), one per run

## Output JSON shape

```
{
  timestamp,
  models: [{provider, model}],
  results: [{
    url, title, kind, fetch_error,
    summaries: [{provider, model, elapsed, summary, error}]
  }]
}
```
