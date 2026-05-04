"""Phase 12.4: Polaris reads agent.provider/model and routes the LLM
call to the matching provider.

Drives `_call_llm` in isolation. Same fake-lightsei trick as
test_polaris_evaluate_push.py: stub the SDK so we can assert what
get_agent_config returns and which call path got taken without spinning
up the real Anthropic / Google clients.

Coverage:
  - No pin (provider=None) → falls back to Anthropic with the env-default
    model.
  - provider=anthropic → calls _call_anthropic with the pinned model id.
  - provider=google → calls _call_gemini with the pinned model id.
  - get_agent_config raising → falls back to Anthropic + env-default
    rather than crashing the tick loop.
  - Unknown provider → raises with a helpful message (not a silent
    fallback).
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


POLARIS_BOT_PATH = (
    Path(__file__).parent.parent.parent / "polaris" / "bot.py"
).resolve()


@pytest.fixture()
def fake_polaris(monkeypatch):
    """Stub `lightsei` and load polaris/bot.py under a unique name so it
    doesn't collide with the @on_command load in test_polaris_evaluate_push.
    """
    fake = types.ModuleType("lightsei")
    fake.send_command = MagicMock(return_value={"id": "x"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    fake.flush = MagicMock()
    fake.get_agent_config = MagicMock(
        return_value={"provider": None, "model": None}
    )
    fake.track = lambda fn: fn
    fake.on_command = lambda *a, **kw: (lambda fn: fn)
    monkeypatch.setitem(sys.modules, "lightsei", fake)

    monkeypatch.delitem(sys.modules, "polaris_bot_routing", raising=False)
    spec = importlib.util.spec_from_file_location(
        "polaris_bot_routing", str(POLARIS_BOT_PATH)
    )
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["polaris_bot_routing"] = bot
    spec.loader.exec_module(bot)

    # Stub the two provider-specific call paths so we can observe which
    # one got picked + with what model, without actually calling Anthropic
    # or Google.
    anthropic_calls: list[tuple[str, dict, str]] = []
    gemini_calls: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        bot,
        "_call_anthropic",
        lambda sp, docs, model: (
            anthropic_calls.append((sp, docs, model))
            or {"input": {"summary": "via anthropic"}, "model": model,
                "tokens_in": 1, "tokens_out": 1, "stop_reason": "end_turn"}
        ),
    )
    monkeypatch.setattr(
        bot,
        "_call_gemini",
        lambda sp, docs, model: (
            gemini_calls.append((sp, docs, model))
            or {"input": {"summary": "via gemini"}, "model": model,
                "tokens_in": 1, "tokens_out": 1, "stop_reason": "stop"}
        ),
    )

    return fake, bot, anthropic_calls, gemini_calls


# ---------- _call_llm routing ---------- #


def test_no_pin_routes_to_anthropic_with_env_default(fake_polaris, monkeypatch):
    fake, bot, anth, gem = fake_polaris
    fake.get_agent_config.return_value = {"provider": None, "model": None}
    monkeypatch.setattr(bot, "MODEL", "claude-opus-4-7", raising=False)

    result = bot._call_llm("sys", {"docs": {"X.md": "x"}})
    assert len(anth) == 1
    assert anth[0][2] == "claude-opus-4-7"
    assert gem == []
    assert result["model"] == "claude-opus-4-7"


def test_pin_anthropic_routes_to_anthropic_with_pinned_model(fake_polaris):
    fake, bot, anth, gem = fake_polaris
    fake.get_agent_config.return_value = {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
    }

    bot._call_llm("sys", {"docs": {"X.md": "x"}})
    assert len(anth) == 1
    assert anth[0][2] == "claude-haiku-4-5"
    assert gem == []


def test_pin_google_routes_to_gemini_with_pinned_model(fake_polaris):
    fake, bot, anth, gem = fake_polaris
    fake.get_agent_config.return_value = {
        "provider": "google",
        "model": "gemini-1.5-flash",
    }

    bot._call_llm("sys", {"docs": {"X.md": "x"}})
    assert len(gem) == 1
    assert gem[0][2] == "gemini-1.5-flash"
    assert anth == []


def test_pin_provider_only_falls_back_to_env_default_model(fake_polaris, monkeypatch):
    """User pinned a provider but not a model. Use the env-default model
    rather than reject — the dashboard's UI nudges them to set both, but
    we don't want a half-pin to brick a tick."""
    fake, bot, anth, gem = fake_polaris
    fake.get_agent_config.return_value = {
        "provider": "anthropic",
        "model": None,
    }
    monkeypatch.setattr(bot, "MODEL", "claude-sonnet-4-6", raising=False)

    bot._call_llm("sys", {"docs": {"X.md": "x"}})
    assert anth[0][2] == "claude-sonnet-4-6"


def test_get_agent_config_failure_falls_back_to_anthropic(fake_polaris, monkeypatch):
    fake, bot, anth, gem = fake_polaris
    fake.get_agent_config.side_effect = RuntimeError("backend down")
    monkeypatch.setattr(bot, "MODEL", "claude-opus-4-7", raising=False)

    bot._call_llm("sys", {"docs": {"X.md": "x"}})
    assert len(anth) == 1
    assert anth[0][2] == "claude-opus-4-7"
    assert gem == []


def test_unknown_provider_raises_helpful_error(fake_polaris):
    fake, bot, anth, gem = fake_polaris
    fake.get_agent_config.return_value = {
        "provider": "groq",
        "model": "llama-3-70b",
    }

    with pytest.raises(RuntimeError) as exc:
        bot._call_llm("sys", {"docs": {"X.md": "x"}})
    msg = str(exc.value).lower()
    assert "groq" in msg
    assert "anthropic" in msg and "google" in msg
    assert anth == [] and gem == []


# ---------- _strip_schema_for_gemini ---------- #


def test_strip_schema_drops_strict_and_additional_properties(fake_polaris):
    _, bot, _, _ = fake_polaris
    schema = {
        "type": "object",
        "additionalProperties": False,
        "strict": True,
        "properties": {
            "name": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"value": {"type": "string"}},
                },
            },
        },
    }
    cleaned = bot._strip_schema_for_gemini(schema)
    assert "additionalProperties" not in cleaned
    assert "strict" not in cleaned
    # Recursively cleaned.
    item_schema = cleaned["properties"]["tags"]["items"]
    assert "additionalProperties" not in item_schema


def test_strip_schema_collapses_anyof_for_nullable_fields(fake_polaris):
    _, bot, _, _ = fake_polaris
    schema = {
        "type": "object",
        "properties": {
            "blocked_by": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "what blocks this action",
            },
        },
    }
    cleaned = bot._strip_schema_for_gemini(schema)
    blocked = cleaned["properties"]["blocked_by"]
    # The null branch is dropped; the string branch's `type` is hoisted.
    assert blocked.get("type") == "string"
    assert "anyOf" not in blocked
