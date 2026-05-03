"""Phase 11.4: Hermes notifier bot.

Drives `agents/hermes/bot.py`'s tick + classify_outcome in isolation.
The bot's lightsei calls and HTTP dispatcher are both injected
seams — the tests stub both so the test suite never makes real
Slack/Discord webhook calls.

Coverage:
  - classify_outcome: 2xx → ok, 4xx → fail, 5xx → retry, transport
    failure (-1 / None) → retry.
  - tick happy path: claim → dispatch → emit hermes.posted →
    complete with `result`.
  - tick uses HERMES_DEFAULT_CHANNEL when payload omits channel.
  - tick: 5xx response triggers a retry; if the retry succeeds,
    final outcome is `posted` and attempt_count = 2.
  - tick: 4xx response goes straight to hermes.send_failed without
    retrying (auth / bad URL needs human action).
  - tick: dispatcher exception classified as retry; if both attempts
    raise, surfaces as hermes.send_failed.
  - tick: empty queue returns None without side effects.
  - tick: unknown command kind completes failed without dispatching.
"""
import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


HERMES_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "hermes" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    """Stub the lightsei module + load Hermes's bot.py via importlib
    so its module name (`hermes_bot`) doesn't collide with
    polaris/bot.py or atlas/bot.py."""
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)

    monkeypatch.delitem(sys.modules, "hermes_bot", raising=False)
    spec = importlib.util.spec_from_file_location(
        "hermes_bot", str(HERMES_BOT_PATH)
    )
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["hermes_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- classify_outcome ---------- #


def test_classify_2xx_is_ok(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_outcome(200) == "ok"
    assert bot.classify_outcome(204) == "ok"
    assert bot.classify_outcome(299) == "ok"


def test_classify_4xx_is_fail(fake_lightsei):
    """4xx is terminal — don't retry on bad auth or malformed URLs."""
    _, bot = fake_lightsei
    assert bot.classify_outcome(401) == "fail"
    assert bot.classify_outcome(404) == "fail"
    assert bot.classify_outcome(429) == "fail"


def test_classify_5xx_is_retry(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_outcome(500) == "retry"
    assert bot.classify_outcome(502) == "retry"
    assert bot.classify_outcome(503) == "retry"


def test_classify_transport_failure_is_retry(fake_lightsei):
    """`None` (no status) and negative codes both signal a transport
    blip — Hermes treats them like a 5xx and retries once."""
    _, bot = fake_lightsei
    assert bot.classify_outcome(None) == "retry"
    assert bot.classify_outcome(-1) == "retry"
    assert bot.classify_outcome(-2) == "retry"


# ---------- tick happy path ---------- #


def _cmd(
    *,
    cmd_id: str = "cmd-1",
    kind: str = "hermes.post",
    payload: dict | None = None,
) -> dict:
    return {
        "id": cmd_id,
        "agent_name": "hermes",
        "kind": kind,
        "payload": payload or {},
        "dispatch_chain_id": "chain-1",
        "approval_state": "auto_approved",
    }


def _delivery(http_status: int = 200) -> dict:
    return {
        "delivery": {
            "channel_id": "ch-1",
            "status": "sent" if 200 <= http_status < 300 else "failed",
            "response_summary": {"http_status": http_status},
            "attempt_count": 1,
        }
    }


def test_tick_happy_path_emits_posted_and_completes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(
        payload={
            "channel": "ops",
            "text": "✅ atlas: 322 passed at commit ede6e01",
            "severity": "info",
        }
    )
    dispatcher = MagicMock(return_value=_delivery(200))

    result = bot.tick(fake, dispatcher, default_channel="default")

    assert result is not None
    dispatcher.assert_called_once_with(
        "ops", "✅ atlas: 322 passed at commit ede6e01", "info"
    )
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "hermes.posted"
    payload = fake.emit.call_args.args[1]
    assert payload["channel"] == "ops"
    assert payload["http_status"] == 200
    assert payload["attempt_count"] == 1
    fake.complete_command.assert_called_once()
    assert "result" in fake.complete_command.call_args.kwargs
    assert (
        fake.complete_command.call_args.kwargs["result"]["http_status"] == 200
    )


def test_tick_uses_default_channel_when_payload_omits_it(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "hi"})
    dispatcher = MagicMock(return_value=_delivery(200))
    bot.tick(fake, dispatcher, default_channel="default-ops")
    args = dispatcher.call_args.args
    assert args[0] == "default-ops"
    assert args[1] == "hi"
    assert args[2] == "info"  # default severity


# ---------- retry path ---------- #


def test_tick_5xx_retries_once_and_succeeds(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "hi"})
    # First call: 503. Second call: 200.
    dispatcher = MagicMock(side_effect=[_delivery(503), _delivery(200)])
    sleep = MagicMock()
    bot.tick(
        fake, dispatcher, default_channel="default", retry_delay_s=0.0,
        sleep=sleep,
    )
    assert dispatcher.call_count == 2
    sleep.assert_called_once_with(0.0)
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "hermes.posted"
    assert fake.emit.call_args.args[1]["attempt_count"] == 2


def test_tick_5xx_retries_once_and_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "hi"})
    dispatcher = MagicMock(side_effect=[_delivery(503), _delivery(503)])
    sleep = MagicMock()
    bot.tick(
        fake, dispatcher, default_channel="default", retry_delay_s=0.0,
        sleep=sleep,
    )
    assert dispatcher.call_count == 2
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "hermes.send_failed"
    assert fake.emit.call_args.args[1]["http_status"] == 503
    fake.complete_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs


def test_tick_4xx_does_not_retry(fake_lightsei):
    """4xx is auth / bad URL — retrying just hits the same wall.
    Skip straight to send_failed with no second attempt."""
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "hi"})
    dispatcher = MagicMock(return_value=_delivery(401))
    sleep = MagicMock()
    bot.tick(
        fake, dispatcher, default_channel="default", retry_delay_s=0.0,
        sleep=sleep,
    )
    assert dispatcher.call_count == 1  # no retry
    sleep.assert_not_called()
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "hermes.send_failed"
    assert fake.emit.call_args.args[1]["http_status"] == 401


# ---------- dispatcher exception ---------- #


def test_tick_dispatcher_exception_retried(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "hi"})
    dispatcher = MagicMock(side_effect=[RuntimeError("boom"), _delivery(200)])
    sleep = MagicMock()
    bot.tick(
        fake, dispatcher, default_channel="default", retry_delay_s=0.0,
        sleep=sleep,
    )
    assert dispatcher.call_count == 2
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "hermes.posted"


def test_tick_dispatcher_exception_both_attempts_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"text": "hi"})
    dispatcher = MagicMock(
        side_effect=[RuntimeError("first"), RuntimeError("second")]
    )
    sleep = MagicMock()
    bot.tick(
        fake, dispatcher, default_channel="default", retry_delay_s=0.0,
        sleep=sleep,
    )
    assert dispatcher.call_count == 2
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "hermes.send_failed"
    assert "second" in (fake.emit.call_args.args[1].get("error") or "")
    fake.complete_command.assert_called_once()


# ---------- empty / unknown ---------- #


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    dispatcher = MagicMock()
    result = bot.tick(fake, dispatcher)
    assert result is None
    dispatcher.assert_not_called()
    fake.emit.assert_not_called()
    fake.complete_command.assert_not_called()


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="hermes.dance")
    dispatcher = MagicMock()
    bot.tick(fake, dispatcher)
    dispatcher.assert_not_called()
    fake.complete_command.assert_called_once()
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]
