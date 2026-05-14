"""Replay captured LLM calls against alternative models for side-by-side comparison.

Reads a JSONL file written by RunCapture and re-issues each LLM call against a
list of provider:model pairs. The exact captured instructions and input are
used — only the model varies.
"""

import asyncio
import json
import logging
import pathlib
from datetime import UTC, datetime

from config import get_api_key_for_provider, load_config
from providers.llm import get_adapter

log = logging.getLogger(__name__)


def _parse_model_spec(spec: str) -> tuple[str, str]:
    if ":" not in spec:
        raise ValueError(f"Model spec must be in 'provider:model' format, got: {spec!r}")
    provider, model = spec.split(":", 1)
    return provider.strip(), model.strip()


def _input_for(call_type: str, record: dict) -> str:
    if call_type == "filter":
        return json.dumps(record.get("payload") or [], ensure_ascii=False)
    if call_type == "summarize":
        return record.get("input") or ""
    if call_type == "search":
        return record.get("prompt") or ""
    raise ValueError(f"Unknown call_type: {call_type!r}")


async def _replay_one(provider: str, model: str, record: dict, api_key_cfg) -> dict:
    call_type = record["call_type"]
    instructions = record.get("instructions")
    web_search = bool(record.get("web_search", False))
    input_text = _input_for(call_type, record)
    adapter = get_adapter(provider, get_api_key_for_provider(api_key_cfg, provider))
    try:
        resp = await adapter.complete(
            input_text,
            model=model,
            instructions=instructions,
            web_search=web_search,
        )
    except Exception as exc:
        return {"model": f"{provider}:{model}", "error": str(exc)}
    if resp is None:
        return {"model": f"{provider}:{model}", "error": "no response"}
    return {
        "model": f"{provider}:{model}",
        "text": resp.text,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "latency_s": resp.latency_s,
        "reasoning": resp.reasoning,
    }


async def _replay_record(record: dict, models: list[tuple[str, str]], api_key_cfg) -> dict:
    results = await asyncio.gather(
        *[_replay_one(p, m, record, api_key_cfg) for p, m in models],
        return_exceptions=False,
    )
    out_results: list[dict] = [
        {
            "model": record.get("model_used") or record.get("model") or "(original)",
            "text": record.get("response"),
            "input_tokens": record.get("input_tokens"),
            "output_tokens": record.get("output_tokens"),
            "latency_s": record.get("latency_s"),
            "reasoning": record.get("reasoning"),
            "is_original": True,
        },
        *results,
    ]
    return {
        "call_type": record["call_type"],
        "task": record.get("task"),
        "ts": record.get("ts"),
        "web_search": bool(record.get("web_search", False)),
        "item_id": record.get("item_id"),
        "item_title": record.get("item_title"),
        "results": out_results,
    }


async def replay(
    jsonl_path: pathlib.Path,
    model_specs: list[str],
    call_filter: str | None,
    state_dir: pathlib.Path,
    config_path: pathlib.Path,
) -> pathlib.Path | None:
    if not jsonl_path.exists():
        raise FileNotFoundError(jsonl_path)
    config = load_config(config_path)
    api_key_cfg = (config.get("llm") or {}).get("api_key") or None
    models = [_parse_model_spec(s) for s in model_specs]

    records: list[dict] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if call_filter and r.get("call_type") != call_filter:
            continue
        records.append(r)

    if not records:
        log.warning("No records matched (call_filter=%r) — nothing to replay.", call_filter)
        return None

    log.info("Replaying %d call(s) against %d model(s)…", len(records), len(models))
    calls = await asyncio.gather(*[_replay_record(r, models, api_key_cfg) for r in records])

    out_dir = state_dir / "evals" / "replays"
    out_dir.mkdir(parents=True, exist_ok=True)
    basename = f"{jsonl_path.parent.name}__{jsonl_path.stem}"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    out_path = out_dir / f"{basename}__replay_{stamp}.json"

    payload = {
        "source_run": str(jsonl_path),
        "replayed_at": datetime.now(UTC).isoformat(),
        "models": [f"{p}:{m}" for p, m in models],
        "call_filter": call_filter,
        "calls": list(calls),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    log.info("Wrote replay output to %s", out_path)
    return out_path
