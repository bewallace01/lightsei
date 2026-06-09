"""Phase 32.4: Marketing assistant (LLM-backed).

Stubs the Anthropic client via the injectable factory so no real Claude
call happens. Mirrors the bot test pattern + the stubbed-Anthropic
harness used by team_planner/agent_generator.
"""
import importlib.util
import sys
import types
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest


MKT_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "marketing" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)
    monkeypatch.delitem(sys.modules, "marketing_bot", raising=False)
    spec = importlib.util.spec_from_file_location("marketing_bot", str(MKT_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["marketing_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _factory(text="Headline!\nBuy now and save.", in_tok=12, out_tok=34):
    """Returns a client_factory(api_key) -> stub anthropic client."""
    block = SimpleNamespace(type="text", text=text)
    resp = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: resp))
    return lambda api_key: client


def _capturing_factory(captured, text="Headline!\nBuy now and save."):
    block = SimpleNamespace(type="text", text=text)
    resp = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=12, output_tokens=34))

    def create(**kwargs):
        captured.update(kwargs)
        return resp

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return lambda api_key: client


# ---------- build_prompt ---------- #


def test_build_prompt_ad_copy_carries_context(fake_lightsei):
    _, bot = fake_lightsei
    system, user = bot.build_prompt("ad_copy", {
        "topic": "summer promo", "platform": "Facebook", "tone": "fun", "business_context": "a cafe"})
    assert "marketing coordinator" in system
    assert "summer promo" in user and "Facebook" in user and "fun" in user and "cafe" in user


def test_build_prompt_varies_by_task(fake_lightsei):
    _, bot = fake_lightsei
    assert "hashtag" in bot.build_prompt("social_post", {"topic": "x"})[1].lower()
    assert "campaign" in bot.build_prompt("campaign_idea", {"topic": "x"})[1].lower()
    assert "subject line" in bot.build_prompt("email_copy", {"topic": "x"})[1].lower()


# ---------- generate_content ---------- #


def test_generate_content_returns_text_and_tokens(fake_lightsei):
    _, bot = fake_lightsei
    out = bot.generate_content("ad_copy", {"topic": "x"}, factory=_factory("Great ad"), api_key="sk", model="m")
    assert out["content"] == "Great ad"
    assert out["input_tokens"] == 12 and out["output_tokens"] == 34


def test_generate_content_passes_timeout(fake_lightsei):
    _, bot = fake_lightsei
    captured = {}
    bot.generate_content(
        "ad_copy", {"topic": "x"}, factory=_capturing_factory(captured),
        api_key="sk", model="m", timeout_s=12.5,
    )
    assert captured["timeout"] == 12.5


def test_generate_content_empty_raises(fake_lightsei):
    _, bot = fake_lightsei
    with pytest.raises(bot.MarketingError):
        bot.generate_content("ad_copy", {"topic": "x"}, factory=_factory(""), api_key="sk", model="m")


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="marketing.create", payload=None):
    return {"id": cmd_id, "agent_name": "marketing", "kind": kind, "payload": payload or {}}


def test_tick_generates_and_notifies(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake.claim_command.return_value = _cmd(payload={"task": "ad_copy", "topic": "summer promo"})
    bot.tick(fake, factory=_factory("Buy now!"), hermes_channel="mkt")
    fake.emit.assert_called_once()
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    out = fake.emit.call_args.args[1]
    assert fake.emit.call_args.args[0] == "marketing.created"
    assert out["content"] == "Buy now!" and out["task"] == "ad_copy"
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and "draft is ready" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "marketing"
    fake.complete_command.assert_called_once()
    assert "result" in fake.complete_command.call_args.kwargs


def test_tick_no_api_key_fails_cleanly(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake.claim_command.return_value = _cmd(payload={"task": "ad_copy", "topic": "x"})
    bot.tick(fake, factory=_factory())
    # No generation; clean error on the command + a crash event, no hermes draft.
    assert fake.emit.call_args.args[0] == "marketing.crash"
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    assert "ANTHROPIC_API_KEY" in fake.complete_command.call_args.kwargs["error"]


def test_tick_unknown_task_fails(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"task": "make_me_rich"})
    bot.tick(fake, factory=_factory())
    assert "unknown marketing task" in fake.complete_command.call_args.kwargs["error"]


def test_tick_unknown_kind_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="marketing.dance")
    bot.tick(fake, factory=_factory())
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake, factory=_factory()) is None


def test_tick_generation_crash_emits_marketing_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"task": "ad_copy", "topic": "x"})
    def boom_factory(api_key):
        raise RuntimeError("anthropic down")
    bot.tick(fake, factory=boom_factory)
    assert fake.emit.call_args.args[0] == "marketing.crash"
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
