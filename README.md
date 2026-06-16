# claudinho

A personal notifier that polls feeds and posts to Discord webhooks (or local markdown files) on a cron schedule.

## Task types

- **RSS** — fetches feeds, posts new entries as Discord embeds with OG images.
- **Digest** — like RSS but collects all passing entries into a single text message (splits at 2000 chars). No OG images. Uses `[Title](<url>)` to suppress Discord link previews.
- **Real-estate** — structured listings from real-estate portals (vivareal / portal-a / portal-b) via vasco's `realestate` adapter (HTTP-first with auto-escalation to browser on bot-blocked pages); posts new listings as Discord embeds.
- **Research** — an agentic loop over vasco's real search + `fetch`/`extract` (via vascod): the LLM searches, reads promising pages, then posts a synthesized, cited plain-text answer (DeepSeek primary, Gemini fallback).
- **Weather** — pulls the daily forecast from Open-Meteo (no API key) and posts a `wttr.in`-style plain-text report. `kind: smart` switches to a signal-only variant that only surfaces dangerous UV windows, significant rain, and σ-based apparent-temp / humidity anomalies vs the location's monthly climate normal.
- **Finance** — yfinance quotes. `report` posts a periodic snapshot (current price, weekly change, 52w range); `monitor` posts intraday alerts when prices move past a `delta` threshold or cross a `price: [low, high]` band. Monitor rules are gated by exchange hours.

RSS and Digest tasks support an optional LLM curate step that classifies items before posting.

Each task can push to any combination of `discord` (webhook) and `file` (local markdown) targets.

## Setup

Requires [uv](https://github.com/astral-sh/uv) and a running **`vascod`** (the resident vasco daemon — sibling project) for content/listing fetches. claudinho talks to it over a UNIX socket; it does not embed vasco. Start it with `systemctl --user enable --now vascod.service` (see vasco's `contrib/systemd/`). If vascod isn't reachable, fetches return `None` and those items are skipped that run.

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
uv run main.py --verbose               # verbose output
```

Config: `$XDG_CONFIG_HOME/claudinho/config.yaml` (default `~/.config/claudinho/config.yaml`)  
State: `$XDG_DATA_HOME/claudinho/state.json` (default `~/.local/share/claudinho/state.json`)  
Logs: `<state_dir>/logs/<timestamp>.log`  
Eval traces: `<state_dir>/evals/<task>/<run_iso>.jsonl` (one record per LLM call per run)

Both paths can be overridden with `--config` and `--state`.

## Eval data

Every run writes a JSONL of its LLM calls (prompts, responses, tokens, latency, optional reasoning trace) under `<state_dir>/evals/<task>/<run_iso>.jsonl`. Use these to spot-check what the LLM saw and said on any past run — for a curate call that includes the per-item verdicts and reasons, the coverage briefing (topic states), and (when `curate.corroborate` is on) the search trajectory and prompt-cache hit/miss counts.

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

### Research task

An agentic loop that drives vasco's real search + `fetch`/`extract` over vascod — the LLM searches, reads promising pages, then synthesizes a cited answer. No provider `web_search`.

```yaml
research: # optional global default model(s); array form = fallback chain
  model:
    - { provider: deepseek, name: deepseek-v4-flash }
    - { provider: gemini, name: gemini-2.5-flash } # fallback, e.g. for prompts DeepSeek refuses

tasks:
  - name: world-news
    period: 24h
    pull:
      - research:
          prompt: "Today's world news. Filter for signal > noise. Summarize with sources."
          # max_steps: 6      # optional loop limits (termination guarantees, not cost tracking)
          # max_searches: 3
          # max_reads: 6
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
    provider: deepseek # one of: deepseek, gemini
    name: deepseek-v4-flash
    reasoning: low # optional: off|low|medium|high (only for thinking-capable models)
  language: "PT-BR" # optional; language for the coverage briefing (topic states)
  explain: true # optional; use filter_reason as item body
```

`model:` also accepts a list — entries are tried in order and the first non-None response wins (fallback chain).

The filter is fail-open: if the LLM call fails twice, all items pass.

## Heuristic processing

Per-feed / per-task / global cleanup, applied before the LLM filter. Four blocks, each
layered global → task → feed (more specific scope wins per leaf key):

```yaml
ignore: # omit a whole FIELD
  image: true # don't fetch / attach the og:image
  description: true # drop the entry description/body
skip: # omit a whole ENTRY
  shorts: true # YouTube /shorts/ entries (self-gates to YouTube)
  livestreams: true # YouTube livestreams / past-stream VODs (self-gates)
  url_contains: ["/tag/"] # entries whose URL contains any substring (str or list)
description: # regex transforms on the description (after HTML-stripping)
  remove: ["Points:.*", "# Comments:.*"] # re.sub each pattern out (str or list)
  extract: "\\d+ de \\w+" # keep only the match (group 1 if captured, else group 0)
  replace: "Imagem do dia\\s*" # re.sub(replace, with, text)
  with: ""
title: # same transform shape, applied to the title
  remove: "\\s*\\| Sponsored$"
```

`ignore` = *don't include this field*; `skip` = *don't include this entry*. A `youtube:` block
takes the same `ignore`/`skip` vocabulary but applies **only to YouTube feeds**, e.g. clear the
description on every YouTube channel with one global lever:

```yaml
youtube:
  skip: { shorts: true, livestreams: true }
  ignore: { description: true }
```
