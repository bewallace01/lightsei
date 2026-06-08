"""Phase 32.6: Business-Intelligence assistant (LLM-backed)."""
import importlib.util
import sys
import types
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest


BI_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "bi" / "bot.py"
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
    monkeypatch.delitem(sys.modules, "bi_bot", raising=False)
    spec = importlib.util.spec_from_file_location("bi_bot", str(BI_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["bi_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _factory(text="Headline: a good week.", in_tok=20, out_tok=60):
    block = SimpleNamespace(type="text", text=text)
    resp = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: resp))
    return lambda api_key: client


# ---------- build_prompt ---------- #


def test_build_prompt_summary_mode(fake_lightsei):
    _, bot = fake_lightsei
    system, user = bot.build_prompt({"period": "this week", "data": {"leads": 12}})
    assert "analyst" in system
    assert "weekly summary" in user.lower()
    assert "this week" in user and "leads" in user and "12" in user


def test_build_prompt_question_mode(fake_lightsei):
    _, bot = fake_lightsei
    _, user = bot.build_prompt({"question": "How many leads?", "data": {"leads": 5}})
    assert "Question to answer: How many leads?" in user


def test_build_prompt_accepts_string_data(fake_lightsei):
    _, bot = fake_lightsei
    _, user = bot.build_prompt({"data": "raw csv text here"})
    assert "raw csv text here" in user


# ---------- generate_summary ---------- #


def test_generate_summary_returns_text_and_tokens(fake_lightsei):
    _, bot = fake_lightsei
    out = bot.generate_summary({"data": {"x": 1}}, factory=_factory("All good"), api_key="sk", model="m")
    assert out["summary"] == "All good"
    assert out["input_tokens"] == 20 and out["output_tokens"] == 60


def test_generate_summary_empty_raises(fake_lightsei):
    _, bot = fake_lightsei
    with pytest.raises(bot.BIError):
        bot.generate_summary({"data": {}}, factory=_factory(""), api_key="sk", model="m")


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="bi.summarize", payload=None):
    return {"id": cmd_id, "agent_name": "bi", "kind": kind, "payload": payload or {}}


def test_tick_summary_emits_and_notifies(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    fake.claim_command.return_value = _cmd(payload={"period": "this week", "data": {"leads": 12}})
    bot.tick(fake, factory=_factory("Weekly read"), hermes_channel="bi")
    out = fake.emit.call_args.args[1]
    assert fake.emit.call_args.args[0] == "bi.summary"
    assert out["kind"] == "summary" and out["summary"] == "Weekly read"
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and "summary is ready" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "bi"


def test_tick_question_mode_marks_answer(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"question": "How many leads?", "data": {"leads": 5}})
    bot.tick(fake, factory=_factory("5 leads."))
    assert fake.emit.call_args.args[1]["kind"] == "answer"
    assert "answered your question" in fake.send_command.call_args.args[2]["text"]


def test_tick_no_api_key_fails_cleanly(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake.claim_command.return_value = _cmd(payload={"data": {}})
    bot.tick(fake, factory=_factory())
    assert fake.emit.call_args.args[0] == "bi.crash"
    assert "ANTHROPIC_API_KEY" in fake.complete_command.call_args.kwargs["error"]


def test_tick_unknown_kind_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="bi.dance")
    bot.tick(fake, factory=_factory())
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake, factory=_factory()) is None


def test_tick_crash_emits_bi_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"data": {}})
    def boom(api_key):
        raise RuntimeError("down")
    bot.tick(fake, factory=boom)
    assert fake.emit.call_args.args[0] == "bi.crash"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
