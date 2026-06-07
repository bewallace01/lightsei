"""Phase 13.4: Cassiopeia incident-scribe bot.

Drives `agents/cassiopeia/bot.py`'s formatting, timeline accrual, and
tick in isolation with a stubbed `lightsei` module.

Coverage:
  - format_entry: actor/message/at handling + defaults.
  - compose_summary: header + bullet list.
  - tick(): open milestone -> emit + dispatch error + complete.
  - tick(): close milestone -> emit + dispatch info; entry_count grows.
  - tick(): non-milestone entry -> emit, no dispatch; timeline accrues.
  - tick(): empty / unknown-kind / crash paths.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


CASS_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "cassiopeia" / "bot.py"
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

    monkeypatch.delitem(sys.modules, "cassiopeia_bot", raising=False)
    spec = importlib.util.spec_from_file_location("cassiopeia_bot", str(CASS_BOT_PATH))
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["cassiopeia_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- format_entry / compose_summary ---------- #


def test_format_entry_with_timestamp(fake_lightsei):
    _, bot = fake_lightsei
    line = bot.format_entry({"actor": "atlas", "message": "tests failing", "at": "2026-06-07T03:00:00Z"})
    assert line == "[2026-06-07T03:00:00Z] atlas: tests failing"


def test_format_entry_defaults(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.format_entry({"message": "boom"}) == "system: boom"
    assert bot.format_entry({"actor": "vega", "action": "reviewed"}) == "vega: reviewed"


def test_compose_summary(fake_lightsei):
    _, bot = fake_lightsei
    s = bot.compose_summary("INC-1", ["a: x", "b: y"])
    assert "Incident INC-1 — 2 entries" in s
    assert "  - a: x" in s and "  - b: y" in s
    assert "1 entry" in bot.compose_summary("INC-2", ["only"])


# ---------- tick ---------- #


def _make_command(*, cmd_id="cmd-1", kind="cassiopeia.record", payload=None):
    return {"id": cmd_id, "agent_name": "cassiopeia", "kind": kind, "payload": payload or {}}


def test_tick_open_milestone_dispatches_error(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"incident_id": "INC-9", "event": {"actor": "sirius", "message": "DB down", "status": "opened"}}
    )
    bot.tick(fake, hermes_channel="incidents")
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "cassiopeia.timeline_entry"
    out = fake.emit.call_args.args[1]
    assert out["incident_id"] == "INC-9"
    assert out["entry_count"] == 1
    assert out["milestone"] is True
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and kind == "hermes.post"
    assert hp["severity"] == "error"
    assert fake.send_command.call_args.kwargs["source_agent"] == "cassiopeia"


def test_tick_accrues_timeline_and_closes(fake_lightsei):
    fake, bot = fake_lightsei
    # entry 1: opened (milestone)
    fake.claim_command.return_value = _make_command(
        payload={"incident_id": "INC-5", "event": {"message": "started", "status": "opened"}}
    )
    bot.tick(fake)
    # entry 2: non-milestone update
    fake.send_command.reset_mock(); fake.emit.reset_mock()
    fake.claim_command.return_value = _make_command(
        payload={"incident_id": "INC-5", "event": {"actor": "atlas", "message": "investigating"}}
    )
    bot.tick(fake)
    assert fake.emit.call_args.args[1]["entry_count"] == 2
    assert fake.emit.call_args.args[1]["milestone"] is False
    fake.send_command.assert_not_called()  # silent middle
    # entry 3: resolved (milestone)
    fake.send_command.reset_mock(); fake.emit.reset_mock()
    fake.claim_command.return_value = _make_command(
        payload={"incident_id": "INC-5", "event": {"message": "fixed", "status": "resolved"}}
    )
    bot.tick(fake)
    out = fake.emit.call_args.args[1]
    assert out["entry_count"] == 3
    assert out["milestone"] is True
    hp = fake.send_command.call_args.args[2]
    assert hp["severity"] == "info"
    assert "resolved" in hp["text"] and "3 entries" in hp["text"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(kind="cassiopeia.dance")
    bot.tick(fake)
    fake.emit.assert_not_called()
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_crash_emits_cassiopeia_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(payload={"incident_id": "x", "event": {"message": "y"}})
    monkeypatch.setattr(bot, "format_entry", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake)
    assert fake.emit.call_args.args[0] == "cassiopeia.crash"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
