"""Phase 32.4: Inbox assistant (LLM triager with structured output)."""
import importlib.util
import json
import sys
import types
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest


INBOX_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "inbox" / "bot.py"
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
    monkeypatch.delitem(sys.modules, "inbox_bot", raising=False)
    spec = importlib.util.spec_from_file_location("inbox_bot", str(INBOX_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["inbox_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _factory(triage: dict, in_tok=15, out_tok=40, *, raw=None):
    text = raw if raw is not None else json.dumps(triage)
    block = SimpleNamespace(type="text", text=text)
    resp = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: resp))
    return lambda api_key: client


# ---------- build_prompt / parse_triage ---------- #


def test_build_prompt_includes_email(fake_lightsei):
    _, bot = fake_lightsei
    system, user = bot.build_prompt({"from": "a@b.com", "subject": "Hi", "body": "hello there"})
    assert "inbox assistant" in system
    assert "a@b.com" in user and "Hi" in user and "hello there" in user


def test_build_prompt_tailors_system_to_industry(fake_lightsei):
    _, bot = fake_lightsei
    system, _ = bot.build_prompt(
        {"from": "a@b.com", "subject": "Hi", "body": "x"},
        industry="home_services",
    )
    assert "home services" in system
    base, _ = bot.build_prompt(
        {"from": "a@b.com", "subject": "Hi", "body": "x"}, industry=None)
    assert "home services" not in base


def test_parse_triage_normalizes_and_validates(fake_lightsei):
    _, bot = fake_lightsei
    t = bot.parse_triage('{"category":"SALES","urgency":"weird","summary":"x","draft_reply":"y","needs_human":1}')
    assert t["category"] == "sales"        # lowercased + valid
    assert t["urgency"] == "normal"        # invalid -> default
    assert t["needs_human"] is True        # coerced to bool


def test_parse_triage_strips_code_fences(fake_lightsei):
    _, bot = fake_lightsei
    fenced = '```json\n{"category":"support","urgency":"high","summary":"s","draft_reply":"","needs_human":true}\n```'
    t = bot.parse_triage(fenced)
    assert t["category"] == "support" and t["urgency"] == "high"


# ---------- generate_triage ---------- #


def test_generate_triage_returns_fields_and_tokens(fake_lightsei):
    _, bot = fake_lightsei
    out = bot.generate_triage(
        {"subject": "x"},
        factory=_factory({"category": "billing", "urgency": "high", "summary": "double charge",
                          "draft_reply": "Sorry!", "needs_human": True}),
        api_key="sk", model="m")
    assert out["category"] == "billing" and out["urgency"] == "high"
    assert out["draft_reply"] == "Sorry!" and out["needs_human"] is True
    assert out["input_tokens"] == 15 and out["output_tokens"] == 40


def test_generate_triage_unparseable_raises(fake_lightsei):
    _, bot = fake_lightsei
    with pytest.raises(bot.InboxError):
        bot.generate_triage({"subject": "x"}, factory=_factory({}, raw="not json at all"),
                            api_key="sk", model="m")


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="inbox.process", payload=None):
    return {"id": cmd_id, "agent_name": "inbox", "kind": kind, "payload": payload or {}}


def test_tick_urgent_email_pages_owner(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"from": "c@x.com", "subject": "Refund", "body": "charged twice"})
    bot.tick(fake, factory=_factory({"category": "billing", "urgency": "high",
                                     "summary": "double charge", "draft_reply": "Sorry!", "needs_human": True}),
             hermes_channel="inbox")
    out = fake.emit.call_args.args[1]
    assert fake.emit.call_args.args[0] == "inbox.processed"
    assert out["category"] == "billing" and out["severity"] == "error"
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and hp["severity"] == "error" and "urgent" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "inbox"


def test_tick_normal_email_no_page(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"subject": "newsletter"})
    bot.tick(fake, factory=_factory({"category": "other", "urgency": "low",
                                     "summary": "a newsletter", "draft_reply": "", "needs_human": False}))
    out = fake.emit.call_args.args[1]
    assert out["severity"] == "info"
    fake.send_command.assert_not_called()  # triaged + drafted silently


def test_tick_no_api_key_fails_cleanly(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake.claim_command.return_value = _cmd(payload={"subject": "x"})
    bot.tick(fake, factory=_factory({}))
    assert fake.emit.call_args.args[0] == "inbox.crash"
    assert "ANTHROPIC_API_KEY" in fake.complete_command.call_args.kwargs["error"]


def test_tick_unknown_kind_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="inbox.dance")
    bot.tick(fake, factory=_factory({}))
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake, factory=_factory({})) is None


def test_tick_unparseable_triage_emits_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={"subject": "x"})
    bot.tick(fake, factory=_factory({}, raw="garbage"))
    assert fake.emit.call_args.args[0] == "inbox.crash"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
