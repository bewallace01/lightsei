"""Phase 11.3: Atlas test-runner bot.

Drives `agents/atlas/bot.py`'s tick + outcome parsing in isolation.
The bot's pytest invocation and the lightsei SDK calls are both
mockable seams — the tests inject stubs so we never recursively
run pytest inside pytest, and so the SDK doesn't actually need to
talk to a backend.

Coverage:
  - build_outcome handles passed / failed / mixed / zero-tests pytest
    summaries and infers severity from returncode.
  - tick(): claim → run → emit → dispatch → complete on the happy path,
    including hermes_text_for formatting that goes onto the wire.
  - tick(): empty queue returns None without side effects.
  - tick(): unknown command kind completes-as-failed without running
    pytest.
  - tick(): runner crash emits atlas.crash + dispatches an error
    hermes.post + completes with the error message.
  - tick(): timeout path emits atlas.crash with the timeout reason.
  - tick(): hermes dispatch failure doesn't block the command's own
    completion (the outcome event is already on the wire).
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------- Build a fake `lightsei` module so importing bot.py
# doesn't require the real SDK to be installed in this test env.
# We replace the dispatch surface with mocks the tests can inspect.
# Reset on every test via a fixture so behavior doesn't bleed
# between cases.

ATLAS_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "atlas" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    """Stub out the entire `lightsei` module with mockable callables
    and load Atlas's bot.py from its file path. We can't just put
    `agents/atlas` on the pytest pythonpath because polaris/bot.py
    already lives at `bot` on it; loading via importlib gives Atlas
    a unique module name (`atlas_bot`) that doesn't collide.
    """
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)

    # Drop any previously-loaded copy so each test gets a fresh import
    # against the just-stubbed lightsei.
    monkeypatch.delitem(sys.modules, "atlas_bot", raising=False)
    spec = importlib.util.spec_from_file_location(
        "atlas_bot", str(ATLAS_BOT_PATH)
    )
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["atlas_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- build_outcome ---------- #


def test_build_outcome_passed_summary(fake_lightsei):
    _, bot = fake_lightsei
    stdout = """
============================= test session starts ==============================
platform darwin -- Python 3.12
collected 12 items

backend/tests/foo.py ............                                       [100%]

============================== 12 passed in 0.85s ==============================
"""
    o = bot.build_outcome(
        stdout=stdout, stderr="", returncode=0, duration_s=1.2
    )
    assert o["passed"] == 12
    assert o["failed"] == 0
    assert o["severity"] == "info"
    # Parsed duration overrides the wall-clock one when pytest's summary
    # had a number we could read.
    assert o["duration_s"] == 0.85
    assert "12 passed" in o["summary"]


def test_build_outcome_failed_summary(fake_lightsei):
    _, bot = fake_lightsei
    stdout = (
        "============================= test session starts ==============================\n"
        "FAILED tests/foo.py::test_bar - assert 1 == 2\n"
        "========================= 4 failed, 8 passed in 1.23s ==========================\n"
    )
    o = bot.build_outcome(
        stdout=stdout, stderr="", returncode=1, duration_s=2.0
    )
    assert o["passed"] == 8
    assert o["failed"] == 4
    assert o["severity"] == "error"
    assert o["returncode"] == 1


def test_build_outcome_unparseable_falls_back_to_returncode(fake_lightsei):
    _, bot = fake_lightsei
    o = bot.build_outcome(
        stdout="some weird output", stderr="", returncode=2, duration_s=0.1
    )
    # Couldn't parse counts — they default to 0.
    assert o["passed"] == 0
    assert o["failed"] == 0
    # Severity inferred from returncode anyway.
    assert o["severity"] == "error"
    # Wall-clock duration used when the summary line didn't have one.
    assert o["duration_s"] == 0.1


def test_build_outcome_log_tail_capped(fake_lightsei, monkeypatch):
    _, bot = fake_lightsei
    monkeypatch.setattr(bot, "LOG_TAIL_BYTES", 32)
    big_stdout = "X" * 1000
    o = bot.build_outcome(
        stdout=big_stdout, stderr="", returncode=0, duration_s=0.1
    )
    assert len(o["log_tail"]) == 32


# ---------- hermes_text_for ---------- #


def test_hermes_text_includes_commit_short_sha(fake_lightsei):
    _, bot = fake_lightsei
    outcome = {
        "passed": 322,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "severity": "info",
    }
    text = bot.hermes_text_for(outcome, commit="ede6e01572f7abd6089c34db5fdf2abcf61f5ae0")
    assert text.startswith("✅ atlas:")
    assert "322 passed" in text
    assert "ede6e01" in text  # short SHA only
    assert "572f7abd" not in text


def test_hermes_text_failure_uses_red_x(fake_lightsei):
    _, bot = fake_lightsei
    outcome = {
        "passed": 318,
        "failed": 4,
        "errors": 0,
        "skipped": 0,
        "severity": "error",
    }
    text = bot.hermes_text_for(outcome, commit=None)
    assert text.startswith("❌ atlas:")
    assert "4 failed" in text


# ---------- tick happy path ---------- #


def _make_command(
    *,
    cmd_id: str = "cmd-1",
    kind: str = "atlas.run_tests",
    payload: dict | None = None,
) -> dict:
    return {
        "id": cmd_id,
        "agent_name": "atlas",
        "kind": kind,
        "payload": payload or {},
        "dispatch_chain_id": "chain-1",
        "approval_state": "auto_approved",
    }


def test_tick_runs_pytest_and_dispatches_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    cmd = _make_command(payload={"commit": "abc1234567"})
    fake.claim_command.return_value = cmd

    runner = MagicMock(return_value={
        "stdout": "============================== 5 passed in 0.10s ==============================",
        "stderr": "",
        "returncode": 0,
        "duration_s": 0.15,
        "timed_out": False,
    })

    result = bot.tick(fake, runner, hermes_channel="ops")

    # The command was claimed.
    assert result is cmd
    # pytest got run with the default args (no override on payload).
    runner.assert_called_once()
    # The atlas.tests_run event went out with the right shape.
    fake.emit.assert_called_once()
    kind, payload = fake.emit.call_args.args[0], fake.emit.call_args.args[1]
    assert kind == "atlas.tests_run"
    assert payload["passed"] == 5
    assert payload["severity"] == "info"
    assert payload["commit"] == "abc1234567"
    # Hermes got dispatched.
    fake.send_command.assert_called_once()
    target, target_kind, hermes_payload = fake.send_command.call_args.args[:3]
    assert target == "hermes"
    assert target_kind == "hermes.post"
    assert hermes_payload["channel"] == "ops"
    assert hermes_payload["severity"] == "info"
    assert "5 passed" in hermes_payload["text"]
    assert "abc1234" in hermes_payload["text"]  # short SHA
    # source_agent on the dispatch is atlas — Phase 11.2 reads this for
    # the constellation's edges + per-day cap.
    assert fake.send_command.call_args.kwargs["source_agent"] == "atlas"
    # Command completed successfully.
    fake.complete_command.assert_called_once()
    cmplete_id = fake.complete_command.call_args.args[0]
    assert cmplete_id == "cmd-1"
    assert "result" in fake.complete_command.call_args.kwargs


def test_tick_payload_pytest_args_overrides_default(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(
        payload={"pytest_args": "tests/test_specific.py"}
    )
    runner = MagicMock(return_value={
        "stdout": "============================== 1 passed in 0.05s ==============================",
        "stderr": "",
        "returncode": 0,
        "duration_s": 0.06,
        "timed_out": False,
    })
    bot.tick(fake, runner)
    runner.assert_called_once_with("tests/test_specific.py")


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    runner = MagicMock()
    result = bot.tick(fake, runner)
    assert result is None
    runner.assert_not_called()
    fake.emit.assert_not_called()
    fake.send_command.assert_not_called()
    fake.complete_command.assert_not_called()


def test_tick_unknown_kind_completes_failed_without_running(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(kind="atlas.do_a_dance")
    runner = MagicMock()
    bot.tick(fake, runner)
    runner.assert_not_called()
    fake.complete_command.assert_called_once()
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


# ---------- tick failure paths ---------- #


def test_tick_runner_crash_emits_atlas_crash_event(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command()
    runner = MagicMock(side_effect=RuntimeError("ohno"))
    bot.tick(fake, runner)
    fake.emit.assert_called_once()
    kind = fake.emit.call_args.args[0]
    assert kind == "atlas.crash"
    payload = fake.emit.call_args.args[1]
    assert "RuntimeError" in payload["error"]
    # Hermes still gets a heads-up message on the crash path.
    fake.send_command.assert_called_once()
    hermes_payload = fake.send_command.call_args.args[2]
    assert hermes_payload["severity"] == "error"
    assert "crashed" in hermes_payload["text"]
    fake.complete_command.assert_called_once()
    assert "RuntimeError" in fake.complete_command.call_args.kwargs["error"]


def test_tick_timeout_path_emits_crash_with_timeout_reason(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command()
    runner = MagicMock(return_value={
        "stdout": "",
        "stderr": "",
        "returncode": -1,
        "duration_s": 999,
        "timed_out": True,
    })
    bot.tick(fake, runner)
    kind = fake.emit.call_args.args[0]
    assert kind == "atlas.crash"
    assert fake.emit.call_args.args[1]["error"] == "pytest timed out"
    fake.send_command.assert_called_once()
    assert "timed out" in fake.send_command.call_args.args[2]["text"]
    fake.complete_command.assert_called_once()
    assert "timed out" in fake.complete_command.call_args.kwargs["error"]


def test_tick_hermes_dispatch_failure_doesnt_fail_command(fake_lightsei):
    """If Hermes is down or rejects the dispatch, Atlas's own command
    still completes successfully. The atlas.tests_run event is already
    on the wire, so the human-side outcome is recorded even if the
    notification path drops."""
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command()
    fake.send_command.side_effect = RuntimeError("hermes unreachable")
    runner = MagicMock(return_value={
        "stdout": "============================== 3 passed in 0.10s ==============================",
        "stderr": "",
        "returncode": 0,
        "duration_s": 0.12,
        "timed_out": False,
    })
    bot.tick(fake, runner)
    # The tests_run event went out.
    assert fake.emit.call_args.args[0] == "atlas.tests_run"
    # Hermes was attempted.
    fake.send_command.assert_called_once()
    # Command completed with `result`, NOT `error`, because the work
    # itself succeeded.
    fake.complete_command.assert_called_once()
    assert "result" in fake.complete_command.call_args.kwargs
    assert "error" not in fake.complete_command.call_args.kwargs
