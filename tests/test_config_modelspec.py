import logging

import pytest
from pydantic import ValidationError

from config import ModelSpec, resolve_model_specs, validate_config


def test_modelspec_accepts_verbose_form():
    s = ModelSpec.model_validate(
        {"provider": "gemini", "name": "gemini-2.5-flash", "reasoning": "low"}
    )
    assert s.provider == "gemini"
    assert s.name == "gemini-2.5-flash"
    assert s.reasoning == "low"


def test_modelspec_reasoning_optional():
    s = ModelSpec.model_validate({"provider": "gemini", "name": "gemini-2.5-flash"})
    assert s.reasoning is None


def test_modelspec_accepts_claude_cli():
    s = ModelSpec.model_validate({"provider": "claude_cli", "name": "sonnet", "reasoning": "high"})
    assert s.provider == "claude_cli"
    assert s.name == "sonnet"
    assert s.reasoning == "high"


def test_modelspec_rejects_unknown_provider():
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"provider": "cohere", "name": "command-r"})


def test_modelspec_rejects_extra_keys():
    with pytest.raises(ValidationError):
        ModelSpec.model_validate(
            {"provider": "gemini", "name": "gemini-2.5-flash", "temperature": 0.5}
        )


def test_modelspec_rejects_reasoning_on_non_thinking_model():
    with pytest.raises(ValidationError) as exc:
        ModelSpec.model_validate(
            {"provider": "deepseek", "name": "deepseek-chat", "reasoning": "low"}
        )
    assert "does not support thinking" in str(exc.value)


def test_modelspec_allows_reasoning_off_on_non_thinking_model():
    s = ModelSpec.model_validate(
        {"provider": "deepseek", "name": "deepseek-chat", "reasoning": "off"}
    )
    assert s.reasoning == "off"


def test_modelspec_warns_on_unknown_model(caplog):
    with caplog.at_level(logging.WARNING, logger="config"):
        s = ModelSpec.model_validate({"provider": "gemini", "name": "gemini-never-released"})
    assert s.name == "gemini-never-released"
    assert any("not in providers/llm/models.json" in r.message for r in caplog.records)


def test_modelspec_warns_on_deprecated_model(caplog):
    with caplog.at_level(logging.WARNING, logger="config"):
        s = ModelSpec.model_validate({"provider": "deepseek", "name": "deepseek-chat"})
    assert s.name == "deepseek-chat"
    assert any("marked deprecated" in r.message for r in caplog.records)


def test_resolve_model_specs_normalizes_scalar_to_list():
    out = resolve_model_specs({"provider": "deepseek", "name": "deepseek-v4-flash"})
    assert len(out) == 1
    assert out[0].provider == "deepseek"


def test_resolve_model_specs_passes_list_through():
    out = resolve_model_specs(
        [
            {"provider": "deepseek", "name": "deepseek-v4-flash"},
            {"provider": "gemini", "name": "gemini-2.5-flash"},
        ]
    )
    assert [s.provider for s in out] == ["deepseek", "gemini"]


def test_resolve_model_specs_returns_empty_for_none():
    assert resolve_model_specs(None) == []


def test_validate_config_rejects_bad_reasoning_on_non_thinking_model():
    cfg = {
        "curate": {"model": {"provider": "deepseek", "name": "deepseek-chat", "reasoning": "high"}},
        "tasks": [
            {
                "name": "x",
                "pull": [{"feed": {"url": "https://example.com/feed.xml"}}],
                "push": [{"discord": {"webhook": "https://discord.example/wh"}}],
            }
        ],
    }
    errors = validate_config(cfg)
    assert errors
    assert any("does not support thinking" in e for e in errors)
