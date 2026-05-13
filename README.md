# claudinho

A personal notifier that polls feeds and posts to Discord webhooks on a cron schedule.

## Task types

- **RSS** — fetches feeds, posts new entries as Discord embeds with OG images.
- **Digest** — like RSS but collects all passing entries into a single text message (splits at 2000 chars). No OG images. Uses `[Title](<url>)` to suppress Discord link previews.
- **Scraper** — browser-based extraction from JavaScript-heavy sites; posts new listings as Discord embeds.
- **LLM** — calls the OpenAI Responses API with a prompt and `web_search_preview`, posts the plain-text response.

RSS and Digest tasks support an optional LLM filter step that classifies items before posting.

## Setup

Requires [uv](https://github.com/astral-sh/uv).

```sh
uv sync
cp config.yaml.template ~/.config/claudinho/config.yaml
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

See [`config.yaml.template`](config.yaml.template) for all supported keys and their defaults. The four task types:

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
    type: digest
    period: 24h
    llm:
      prompt: "Only keep items about AI and machine learning"
    pull:
      - feed:
          name: My Feed
          url: https://example.com/feed.xml
    push:
      - discord:
          webhook: !secret discord_webhook_my_digest
```

### Scraper task

```yaml
tasks:
  - name: listings
    period: 4h
    pull:
      - scraper:
          adapter: vivareal
          url: "https://www.vivareal.com.br/venda/..."
          max_items: 10
    push:
      - discord:
          webhook: !secret discord_webhook_listings
```

### LLM task

```yaml
tasks:
  - name: world-news
    period: 24h
    pull:
      - llm:
          prompt: "Today's news. Filter for signal > noise."
          web_search: true
    push:
      - discord:
          webhook: !secret discord_webhook_world_news
```

## LLM filter (RSS/Digest)

Add an `llm` block to any RSS or Digest task to classify items before posting:

```yaml
llm:
  prompt: "Only keep items about AI and machine learning"
  model: gpt-4o-mini       # optional; overrides llm.models.reasoning
  language: "PT-BR"        # optional; language for memory briefings
  web_search: true         # optional; let the LLM search for context
  explain: true            # optional; use filter_reason as item body
```

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
