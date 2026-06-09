"""Phase 32.3: Reputation assistant."""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REP_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "reputation" / "bot.py"
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
    monkeypatch.delitem(sys.modules, "reputation_bot", raising=False)
    spec = importlib.util.spec_from_file_location("reputation_bot", str(REP_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["reputation_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- analyze_sentiment ---------- #


def test_low_rating_is_negative(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.analyze_sentiment("ok", rating=1)["sentiment"] == "negative"
    assert bot.analyze_sentiment("", rating=2)["sentiment"] == "negative"


def test_high_rating_is_positive(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.analyze_sentiment("", rating=5)["sentiment"] == "positive"


def test_keywords_can_flip_a_three_star(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.analyze_sentiment("staff were rude and the place was dirty", rating=3)["sentiment"] == "negative"
    assert bot.analyze_sentiment("friendly, helpful, would recommend", rating=3)["sentiment"] == "positive"


def test_text_only_no_rating(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.analyze_sentiment("terrible, worst experience, never again")["sentiment"] == "negative"
    assert bot.analyze_sentiment("amazing and fantastic, love it")["sentiment"] == "positive"
    assert bot.analyze_sentiment("it was fine")["sentiment"] == "neutral"


def test_response_hint_varies(fake_lightsei):
    _, bot = fake_lightsei
    assert "apolog" in bot.draft_response_hint("negative").lower()
    assert "thank" in bot.draft_response_hint("positive").lower()
    assert "feedback" in bot.draft_response_hint("neutral").lower()


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="reputation.check", payload=None):
    return {"id": cmd_id, "agent_name": "reputation", "kind": kind, "payload": payload or {}}


def test_tick_negative_review_alerts(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={
        "author": "Sam", "rating": 1, "source": "Google", "text": "Rude and slow, never again"})
    bot.tick(fake, hermes_channel="reviews")
    fake.emit.assert_called_once()
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    out = fake.emit.call_args.args[1]
    assert out["sentiment"] == "negative" and out["severity"] == "error"
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and hp["severity"] == "error" and "negative review" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "reputation"


def test_tick_positive_review_no_alert(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"rating": 5, "text": "amazing, recommend"})
    bot.tick(fake)
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    assert fake.emit.call_args.args[1]["sentiment"] == "positive"
    fake.send_command.assert_not_called()


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="reputation.dance")
    bot.tick(fake)
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()


def test_tick_crash_emits_reputation_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "x"})
    monkeypatch.setattr(bot, "analyze_sentiment", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake)
    assert fake.emit.call_args.args[0] == "reputation.crash"
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
