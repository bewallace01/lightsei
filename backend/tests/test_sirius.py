"""Phase 13.3: Sirius alert-triager bot.

Drives `agents/sirius/bot.py`'s classification, dedup, and tick in
isolation with a stubbed `lightsei` module.

Coverage:
  - classify_severity: explicit field wins; keyword inference; default.
  - fingerprint_for: explicit vs hashed (source,title).
  - triage_alert maps severity -> action.
  - _is_duplicate windowing.
  - tick(): page (high) -> emit + dispatch error + complete.
  - tick(): notify (medium) -> emit + dispatch info.
  - tick(): log (low) -> emit, no dispatch.
  - tick(): duplicate -> suppress, no dispatch.
  - tick(): empty / unknown-kind / crash paths.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SIRIUS_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "sirius" / "bot.py"
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

    monkeypatch.delitem(sys.modules, "sirius_bot", raising=False)
    spec = importlib.util.spec_from_file_location("sirius_bot", str(SIRIUS_BOT_PATH))
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["sirius_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- classify_severity ---------- #


def test_explicit_severity_wins(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_severity({"severity": "critical", "title": "all fine"}) == "high"
    assert bot.classify_severity({"level": "warning"}) == "medium"
    assert bot.classify_severity({"severity": "info"}) == "low"


def test_keyword_inference(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_severity({"title": "Database outage in prod"}) == "high"
    assert bot.classify_severity({"message": "latency elevated, degraded"}) == "medium"
    assert bot.classify_severity({"message": "job resolved, recovered"}) == "low"


def test_classify_defaults_medium(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_severity({"title": "something happened"}) == "medium"


def test_fingerprint_explicit_vs_hashed(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.fingerprint_for({"fingerprint": "db-down"}) == "db-down"
    fp1 = bot.fingerprint_for({"source": "prom", "title": "X"})
    fp2 = bot.fingerprint_for({"source": "prom", "title": "X"})
    fp3 = bot.fingerprint_for({"source": "prom", "title": "Y"})
    assert fp1 == fp2 != fp3
    assert fp1.startswith("fp_")


def test_triage_maps_action(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.triage_alert({"severity": "critical"})["action"] == "page"
    assert bot.triage_alert({"severity": "warning"})["action"] == "notify"
    assert bot.triage_alert({"severity": "info"})["action"] == "log"


def test_is_duplicate_window(fake_lightsei):
    _, bot = fake_lightsei
    assert bot._is_duplicate("fp", window_s=100, now=1000.0) is False  # first sight
    assert bot._is_duplicate("fp", window_s=100, now=1050.0) is True   # within window
    assert bot._is_duplicate("fp", window_s=100, now=1200.0) is False  # window expired


# ---------- tick ---------- #


def _make_command(*, cmd_id="cmd-1", kind="sirius.triage", payload=None):
    return {"id": cmd_id, "agent_name": "sirius", "kind": kind, "payload": payload or {}}


def test_tick_high_pages_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"title": "DB down", "severity": "critical", "fingerprint": "db"}
    )
    bot.tick(fake, hermes_channel="oncall")
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "sirius.triaged"
    out = fake.emit.call_args.args[1]
    assert out["severity"] == "high" and out["action"] == "page"
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and kind == "hermes.post"
    assert hp["severity"] == "error"
    assert "PAGE" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "sirius"


def test_tick_medium_notifies_info(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"title": "latency elevated", "fingerprint": "lat"}
    )
    bot.tick(fake)
    assert fake.emit.call_args.args[1]["action"] == "notify"
    assert fake.send_command.call_args.args[2]["severity"] == "info"


def test_tick_low_logs_without_dispatch(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"title": "job resolved", "fingerprint": "r"}
    )
    bot.tick(fake)
    assert fake.emit.call_args.args[1]["action"] == "log"
    fake.send_command.assert_not_called()


def test_tick_duplicate_is_suppressed(fake_lightsei):
    fake, bot = fake_lightsei
    cmd = _make_command(payload={"title": "DB down", "severity": "critical", "fingerprint": "db"})
    fake.claim_command.return_value = cmd
    bot.tick(fake)  # first: page
    fake.send_command.reset_mock()
    fake.emit.reset_mock()
    bot.tick(fake)  # second within window: suppress
    assert fake.emit.call_args.args[1]["action"] == "suppress"
    assert fake.emit.call_args.args[1]["duplicate"] is True
    fake.send_command.assert_not_called()


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(kind="sirius.dance")
    bot.tick(fake)
    fake.emit.assert_not_called()
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_crash_emits_sirius_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(payload={"title": "x"})
    monkeypatch.setattr(bot, "triage_alert", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake)
    assert fake.emit.call_args.args[0] == "sirius.crash"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
