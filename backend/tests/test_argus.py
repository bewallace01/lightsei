"""Phase 13.1: Argus security + secret scanner bot.

Drives `agents/argus/bot.py`'s scanner + tick in isolation. The lightsei
SDK calls are a mockable seam: tests inject a stub `lightsei` module so
the bot never needs a real backend.

Coverage:
  - scan_for_secrets catches AWS keys, private-key blocks, provider
    tokens, and generic assignments; masks every matched value so the
    raw secret never leaves the finding; entropy-filters placeholders;
    returns nothing on clean text.
  - hermes_text_for formats a one-line alert with a short SHA.
  - tick(): high finding -> emit + dispatch hermes + complete.
  - tick(): medium-only finding -> emit + NO hermes dispatch + complete.
  - tick(): clean scan -> emit zero findings, no dispatch.
  - tick(): empty queue -> None, no side effects.
  - tick(): unknown kind -> completed-as-failed without scanning.
  - tick(): scanner crash -> emit argus.crash + dispatch error + complete.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ARGUS_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "argus" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    """Stub the `lightsei` module and load Argus's bot.py under a unique
    module name (other bots already occupy `bot` / `atlas_bot` on the
    path), mirroring test_atlas.py."""
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)

    monkeypatch.delitem(sys.modules, "argus_bot", raising=False)
    spec = importlib.util.spec_from_file_location(
        "argus_bot", str(ARGUS_BOT_PATH)
    )
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["argus_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- scan_for_secrets ---------- #


AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # canonical AWS example access key id


def test_scan_catches_aws_access_key_and_masks_it(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.scan_for_secrets(f"key = '{AWS_KEY}'", path="config.py")
    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "aws_access_key_id"
    assert f["severity"] == "high"
    assert f["line"] == 1
    assert f["path"] == "config.py"
    # The raw secret must never appear in the finding.
    assert AWS_KEY not in f["masked"]
    assert f["masked"].startswith("AKIA")
    assert "(20 chars)" in f["masked"]


def test_scan_catches_private_key_block(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.scan_for_secrets("-----BEGIN RSA PRIVATE KEY-----")
    assert any(f["type"] == "private_key_block" and f["severity"] == "high"
               for f in findings)


def test_scan_catches_provider_and_github_tokens(fake_lightsei):
    _, bot = fake_lightsei
    text = (
        "gh = 'ghp_" + "a" * 36 + "'\n"
        "stripe = 'sk_live_" + "b" * 24 + "'\n"
    )
    findings = bot.scan_for_secrets(text)
    types_found = {f["type"] for f in findings}
    assert "github_token" in types_found
    assert "stripe_secret_key" in types_found
    assert all(f["severity"] == "high" for f in findings)


def test_scan_generic_assignment_is_medium(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.scan_for_secrets('api_key = "a1B2c3D4e5F6g7"')
    assert len(findings) == 1
    assert findings[0]["type"] == "generic_secret_assignment"
    assert findings[0]["severity"] == "medium"


def test_scan_entropy_filters_low_entropy_placeholder(fake_lightsei):
    _, bot = fake_lightsei
    # 12+ chars so it matches the length floor, but zero entropy.
    findings = bot.scan_for_secrets('api_key = "aaaaaaaaaaaa"')
    assert findings == []


def test_scan_clean_text_returns_nothing(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.scan_for_secrets("def add(a, b):\n    return a + b") == []
    assert bot.scan_for_secrets("") == []


# ---------- hermes_text_for ---------- #


def test_hermes_text_includes_count_and_short_sha(fake_lightsei):
    _, bot = fake_lightsei
    findings = [
        {"type": "aws_access_key_id", "severity": "high", "line": 4, "path": "config.py", "masked": "AKIA…"},
        {"type": "github_token", "severity": "high", "line": 9, "path": "ci.yml", "masked": "ghp_…"},
    ]
    text = bot.hermes_text_for(findings, commit="ede6e01572f7abd6089c34db5fdf2abcf61f5ae0")
    assert "argus:" in text
    assert "2 hardcoded secrets" in text
    assert "config.py:4" in text
    assert "+1 more" in text
    assert "ede6e01" in text and "572f7abd" not in text


# ---------- tick ---------- #


def _make_command(*, cmd_id="cmd-1", kind="argus.scan", payload=None):
    return {
        "id": cmd_id,
        "agent_name": "argus",
        "kind": kind,
        "payload": payload or {},
        "dispatch_chain_id": "chain-1",
        "approval_state": "auto_approved",
    }


def test_tick_high_finding_emits_and_dispatches_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"text": f"key = '{AWS_KEY}'", "path": "config.py", "commit": "abc1234567"}
    )
    result = bot.tick(fake, hermes_channel="security")

    assert result is not None
    # scan_complete event with the right shape.
    fake.emit.assert_called_once()
    kind, payload = fake.emit.call_args.args[0], fake.emit.call_args.args[1]
    assert kind == "argus.scan_complete"
    assert payload["findings_count"] == 1
    assert payload["high_severity_count"] == 1
    assert payload["severity"] == "error"
    assert payload["commit"] == "abc1234567"
    # The event must not leak the raw secret either.
    assert AWS_KEY not in str(payload)
    # Hermes dispatched with source_agent=argus.
    fake.send_command.assert_called_once()
    target, target_kind, hermes_payload = fake.send_command.call_args.args[:3]
    assert target == "hermes" and target_kind == "hermes.post"
    assert hermes_payload["channel"] == "security"
    assert hermes_payload["severity"] == "error"
    assert fake.send_command.call_args.kwargs["source_agent"] == "argus"
    # Completed with a result.
    fake.complete_command.assert_called_once()
    assert "result" in fake.complete_command.call_args.kwargs


def test_tick_medium_only_does_not_page_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"text": 'api_key = "a1B2c3D4e5F6g7"'}
    )
    bot.tick(fake)
    fake.emit.assert_called_once()
    payload = fake.emit.call_args.args[1]
    assert payload["findings_count"] == 1
    assert payload["high_severity_count"] == 0
    assert payload["severity"] == "info"
    # No human paged for a medium finding.
    fake.send_command.assert_not_called()
    fake.complete_command.assert_called_once()


def test_tick_clean_scan_emits_zero_findings(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"files": [{"path": "ok.py", "content": "x = 1\n"}]}
    )
    bot.tick(fake)
    payload = fake.emit.call_args.args[1]
    assert payload["files_scanned"] == 1
    assert payload["findings_count"] == 0
    fake.send_command.assert_not_called()
    fake.complete_command.assert_called_once()


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()
    fake.send_command.assert_not_called()
    fake.complete_command.assert_not_called()


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(kind="argus.do_a_dance")
    bot.tick(fake)
    fake.emit.assert_not_called()
    fake.complete_command.assert_called_once()
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_scanner_crash_emits_argus_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(payload={"text": "whatever"})
    monkeypatch.setattr(bot, "scan_for_secrets", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake)
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "argus.crash"
    assert "RuntimeError" in fake.emit.call_args.args[1]["error"]
    # Hermes gets an error heads-up on the crash path.
    fake.send_command.assert_called_once()
    assert fake.send_command.call_args.args[2]["severity"] == "error"
    fake.complete_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
