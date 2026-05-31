# claudinho

A personal notifier that polls feeds and posts to Discord webhooks (or local markdown files) on a cron schedule.

## Task types

- **RSS** — fetches feeds, posts new entries as Discord embeds with OG images.
- **Digest** — like RSS but collects all passing entries into a single text message (splits at 2000 chars). No OG images. Uses `[Title](<url>)` to suppress Discord link previews.
- **Real-estate** — structured listings from real-estate portals (vivareal / binda / barreto) via vasco's `realestate` adapter (HTTP-first with auto-escalation to browser on bot-blocked pages); posts new listings as Discord embeds.
- **Search** — calls an LLM with web search enabled and posts the plain-text response.
- **Weather** — pulls the daily forecast from Open-Meteo (no API key) and posts a `wttr.in`-style plain-text report. `kind: smart` switches to a signal-only variant that only surfaces dangerous UV windows, significant rain, and σ-based apparent-temp / humidity anomalies vs the location's monthly climate normal.
- **Finance** — yfinance quotes. `report` posts a periodic snapshot (current price, weekly change, 52w range); `monitor` posts intraday alerts when prices move past a `delta` threshold or cross a `price: [low, high]` band. Monitor rules are gated by exchange hours.

RSS and Digest tasks support an optional LLM curate step that classifies items before posting.

Each task can push to any combination of `discord` (webhook) and `file` (local markdown) targets.

## Setup

Requires [uv](https://github.com/astral-sh/uv).

```sh
uv sync
cp config/config.yaml.template ~/.config/claudinho/config.yaml
cp secrets.yaml.template ~/.config/claudinho/secrets.yaml
# Edit both files
```

Secrets are referenced via `!secret <key>` tags in `config.yaml` and resolved from `secrets.yaml` in the same directory. Alternatively, set `$OPENAI_API_KEY` in the environment.

## Usage

```sh
uv run main.py                         # run all due tasks
uv run main.py --task "world-news"     # run one task by name (ignores period/last_run)
uv run main.py --config config.yaml    # explicit config path
uv run main.py --validate              # validate config and exit
uv run main.py --migrate               # migrate state file to current schema
uv run main.py --clean                 # remove stale state entries, then exit
uv run main.py --stats                 # rich-formatted summary of state.json (per-task/per-source last_run, next_run, item counts)
uv run main.py --summarize <url>       # fetch YouTube transcript and print summary
uv run main.py --analysis --task <t>   # inspection mode: LLM reasoning + ELI5 reasons, dry-run, render to stdout (extra tokens)
uv run main.py --replay <jsonl> --models openai:gpt-4o-mini,gemini:gemini-2.5-flash --call filter
uv run main.py --verbose               # verbose output
```

Config: `$XDG_CONFIG_HOME/claudinho/config.yaml` (default `~/.config/claudinho/config.yaml`)  
State: `$XDG_DATA_HOME/claudinho/state.json` (default `~/.local/share/claudinho/state.json`)  
Logs: `<state_dir>/logs/<timestamp>.log`  
Eval traces: `<state_dir>/evals/<task>/<run_iso>.jsonl` (one record per LLM call per run)

Both paths can be overridden with `--config` and `--state`.

## Eval data

Every run writes a JSONL of its LLM calls (prompts, responses, tokens, latency, optional reasoning trace) under `<state_dir>/evals/<task>/<run_iso>.jsonl`. Use these to spot-check what the LLM saw and said on any past run, or feed them to `--replay` to compare against alternative models. The replay command re-issues each captured call against the listed `provider:model` pairs using the exact same instructions and input, so the only variable is the model:

```sh
uv run main.py --replay ~/.local/share/claudinho/evals/world-news/2026-05-13T08-00-00.jsonl \
  --models openai:gpt-4o-mini,gemini:gemini-2.5-flash \
  --call filter
```

Output is written to `<state_dir>/evals/replays/<basename>__replay_<ts>.json` with the original captured response included as the reference.

## Cron example

```cron
*/15 * * * * cd /path/to/claudinho && uv run main.py >> /tmp/claudinho.log 2>&1
```

## Config reference

See [`config/config.yaml.template`](config/config.yaml.template) for all supported keys and their defaults. The task types:

### RSS task

```yaml
tasks:
  - name: my-feeds
    period: 1h
    pull:
      - feed:
          name: My Feed
          url: https://example.com/feed.xml
    push:
      - discord:
          webhook: !secret discord_webhook_my_feeds
```

### Digest task

```yaml
tasks:
  - name: my-digest
    kind: digest
    period: 24h
    curate:
      criteria: "Only keep items about AI and machine learning"
    pull:
      - feed:
          name: My Feed
          url: https://example.com/feed.xml
    push:
      - discord:
          webhook: !secret discord_webhook_my_digest
```

### Real-estate task

```yaml
tasks:
  - name: listings
    period: 4h
    pull:
      - realestate:
          url: "https://www.vivareal.com.br/venda/..."
          max_items: 10
    push:
      - discord:
          webhook: !secret discord_webhook_listings
```

### Search task

```yaml
tasks:
  - name: world-news
    period: 24h
    pull:
      - search:
          prompt: "Today's news. Filter for signal > noise."
    push:
      - discord:
          webhook: !secret discord_webhook_world_news
```

### Weather task

```yaml
tasks:
  - name: weather
    period: 1d
    pull:
      - weather:
          # kind: smart            # optional; signal-only variant (anomalies + significant rain only)
          latitude: -23.55
          longitude: -46.63
          location_name: "São Paulo, SP"
          timezone: "America/Sao_Paulo"
          uv_warn_threshold: 6 # optional; default 6
          forecast_days: 5 # optional; default 5
    push:
      - discord:
          webhook: !secret discord_webhook_weather
          wrap: false # recommended; weather lines are intentionally long
```

### Finance task

`report` mode — periodic snapshot:

```yaml
tasks:
  - name: finance-report
    period: 7d
    pull:
      - finance:
          report:
            stocks: [AAPL, MSFT, KO, SPY, "EURUSD=X"]
    push:
      - discord:
          webhook: !secret discord_webhook_finance
          wrap: false
```

`monitor` mode — intraday alerts on `delta` moves and/or `price` band crossings. Silent on zero-alert ticks. Each rule is gated by exchange hours (DST-aware); exchange is inferred from the symbol suffix (`.SA` → b3, `=X`/`.NYB` → fx, `X-USD` → crypto, else us_equity):

```yaml
tasks:
  - name: finance-monitor
    period: 15m
    pull:
      - finance:
          monitor:
            - { ticker: "EURUSD=X", delta: 0.005 }
            - { ticker: AAPL, delta: 0.05 }
            - { ticker: NVDA, delta: 0.05, price: [800, 950] }
            - { ticker: "BTC-USD", delta: 0.02 }
    push:
      - discord:
          webhook: !secret discord_webhook_finance
          wrap: false
```

## LLM curation (RSS/Digest)

Add a `curate` block to any RSS or Digest task to classify items before posting:

```yaml
curate:
  criteria: "Only keep items about AI and machine learning"
  model: # optional; overrides global curate.model
    provider: openai # one of: openai, gemini, anthropic, deepseek
    name: gpt-5.4-mini
    reasoning: low # optional: off|low|medium|high (only for thinking-capable models)
  language: "PT-BR" # optional; language for memory briefings
  web_search: true # optional; let the LLM search for context
  explain: true # optional; use filter_reason as item body
```

`model:` also accepts a list — entries are tried in order and the first non-None response wins (fallback chain).

The filter is fail-open: if the LLM call fails twice, all items pass.

## Heuristic filters

Per-feed or per-task text cleanup, applied before the LLM filter:

```yaml
filter:
  title:
    extract: "\\d+ de \\w+"
  description:
    - remove_phrases_with_urls: true
    - remove_phrases_containing: "Subscribe"
    - replace: "Imagem do dia\\s*"
      with: ""
  url:
    skip_containing: "/shorts/"
```

Supported ops: `extract`, `replace`/`with`, `remove_phrases_with_urls`, `remove_phrases_containing`, `clear`, `skip_containing`.
