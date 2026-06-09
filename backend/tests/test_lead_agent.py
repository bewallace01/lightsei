"""Phase 32.2: Lead-Management assistant.

Drives `agents/lead/bot.py`'s scoring, follow-up logic, and tick in
isolation with a stubbed `lightsei` module.
"""
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


LEAD_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "lead" / "bot.py"
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
    monkeypatch.delitem(sys.modules, "lead_bot", raising=False)
    spec = importlib.util.spec_from_file_location("lead_bot", str(LEAD_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["lead_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _now():
    return datetime.now(timezone.utc)


# ---------- score_lead ---------- #


def test_score_hot_lead(fake_lightsei):
    _, bot = fake_lightsei
    s = bot.score_lead({"name": "Pat", "email": "p@co.com", "phone": "555",
                        "company": "Co", "message": "very interested in a demo, budget $2k/mo"})
    assert s["score"] >= 70 and s["quality"] == "hot"
    assert "message shows buying intent" in s["reasons"]
    assert "mentioned budget" in s["reasons"]


def test_score_cold_lead(fake_lightsei):
    _, bot = fake_lightsei
    s = bot.score_lead({"message": "hi"})  # no contact info, no intent
    assert s["score"] < 40 and s["quality"] == "cold"


def test_score_warm_lead(fake_lightsei):
    _, bot = fake_lightsei
    s = bot.score_lead({"email": "p@co.com", "company": "Co", "message": "just looking around the site"})
    assert s["quality"] == "warm"


def test_score_caps_at_100(fake_lightsei):
    _, bot = fake_lightsei
    s = bot.score_lead({"name": "P", "email": "e", "phone": "p", "company": "c",
                        "message": "urgent demo quote pricing buy now budget $5000", "budget": "5k"})
    assert s["score"] == 100


# ---------- needs_followup ---------- #


def test_needs_followup_never_contacted(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.needs_followup({"status": "new"}, now=_now()) is True


def test_needs_followup_recent_contact_is_false(fake_lightsei):
    _, bot = fake_lightsei
    recent = (_now() - timedelta(hours=2)).isoformat()
    assert bot.needs_followup({"last_contact_at": recent}, now=_now(), window_hours=48) is False


def test_needs_followup_stale_contact_is_true(fake_lightsei):
    _, bot = fake_lightsei
    stale = (_now() - timedelta(hours=72)).isoformat()
    assert bot.needs_followup({"last_contact_at": stale}, now=_now(), window_hours=48) is True


def test_needs_followup_closed_is_false(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.needs_followup({"status": "won"}, now=_now()) is False
    assert bot.needs_followup({"status": "lost"}, now=_now()) is False


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="lead.process", payload=None):
    return {"id": cmd_id, "agent_name": "lead", "kind": kind, "payload": payload or {}}


def test_tick_hot_due_lead_pages_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={
        "name": "Pat", "email": "p@co.com", "phone": "555",
        "message": "want a demo asap, budget $2k", "status": "new"})
    bot.tick(fake, hermes_channel="sales")
    fake.emit.assert_called_once()
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    out = fake.emit.call_args.args[1]
    assert out["quality"] == "hot" and out["needs_followup"] is True
    assert out["severity"] == "error"
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and hp["severity"] == "error"
    assert "Call now" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "lead"


def test_tick_cold_lead_no_page(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"message": "hi", "status": "new"})
    bot.tick(fake)
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    assert fake.emit.call_args.args[1]["quality"] == "cold"
    fake.send_command.assert_not_called()


def test_tick_recently_contacted_warm_no_page(fake_lightsei):
    fake, bot = fake_lightsei
    recent = (_now() - timedelta(hours=1)).isoformat()
    fake.claim_command.return_value = _cmd(payload={
        "email": "p@co.com", "company": "Co", "message": "looking around",
        "status": "contacted", "last_contact_at": recent})
    bot.tick(fake)
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    assert fake.emit.call_args.args[1]["needs_followup"] is False
    fake.send_command.assert_not_called()  # not due -> no page


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="lead.dance")
    bot.tick(fake)
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()


def test_tick_crash_emits_lead_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"email": "e"})
    monkeypatch.setattr(bot, "score_lead", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake)
    assert fake.emit.call_args.args[0] == "lead.crash"
    assert fake.emit.call_args.kwargs["run_id"] == "cmd-1"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
