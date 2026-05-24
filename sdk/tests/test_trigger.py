"""Phase 22.5: tests for the @lightsei.on_trigger decorator + the
read-only lightsei.trigger accessor.

The trigger context is set by the trigger.fire bridge handler (an
internal @on_command('trigger.fire') registered when _trigger is
imported). Tests drive that bridge directly with hand-crafted
payloads so we don't need a fake backend to exercise dispatch +
context setup.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import lightsei
from lightsei import _trigger as trigmod
from lightsei._context import get_run_id


@pytest.fixture(autouse=True)
def _reset_handler():
    """Clear any handler registered by a prior test so registry state
    doesn't bleed across cases."""
    trigmod._handler = None
    yield
    trigmod._handler = None


# ---------- lightsei.trigger accessor (no context) ---------- #


def test_accessor_defaults_to_manual_outside_context():
    """Outside a trigger.fire dispatch, the bot is running manually
    (CLI, dashboard 'Run now', etc). kind == 'manual'; everything
    else is None."""
    assert lightsei.trigger.kind == "manual"
    assert lightsei.trigger.name is None
    assert lightsei.trigger.scheduled_at is None
    assert lightsei.trigger.webhook_payload is None
    assert lightsei.trigger.trigger_id is None


# ---------- @on_trigger decorator ---------- #


def test_on_trigger_no_parens_registers_handler():
    @lightsei.on_trigger
    def handle():
        return {"ran": True}

    assert trigmod.has_trigger_handler()
    assert trigmod.get_trigger_handler() is handle


def test_on_trigger_with_parens_registers_handler():
    @lightsei.on_trigger()
    def handle():
        return None

    assert trigmod.has_trigger_handler()


def test_redecorating_replaces_previous_handler():
    @lightsei.on_trigger
    def first():
        return {"who": "first"}

    @lightsei.on_trigger
    def second():
        return {"who": "second"}

    assert trigmod.get_trigger_handler() is second


# ---------- Bridge: context populated correctly ---------- #


def test_bridge_populates_cron_context():
    """A cron payload sets kind='cron', scheduled_at (parsed from
    ISO), trigger_id + name; webhook_payload stays None."""
    captured = {}

    @lightsei.on_trigger
    def handle():
        captured["kind"] = lightsei.trigger.kind
        captured["name"] = lightsei.trigger.name
        captured["scheduled_at"] = lightsei.trigger.scheduled_at
        captured["webhook_payload"] = lightsei.trigger.webhook_payload
        captured["trigger_id"] = lightsei.trigger.trigger_id
        captured["run_id"] = get_run_id()
        return None

    payload = {
        "run_id": "run-abc",
        "trigger_id": "trig-1",
        "trigger_name": "morning digest",
        "trigger_kind": "cron",
        "scheduled_at": "2026-05-24T09:00:00+00:00",
    }
    result = trigmod._trigger_fire_bridge(payload)
    assert result == {"ok": True}
    assert captured["kind"] == "cron"
    assert captured["name"] == "morning digest"
    assert captured["scheduled_at"] == datetime(
        2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc,
    )
    assert captured["webhook_payload"] is None
    assert captured["trigger_id"] == "trig-1"
    assert captured["run_id"] == "run-abc"


def test_bridge_populates_webhook_context():
    """Webhook payload sets kind='webhook' + webhook_payload; the
    scheduled_at slot stays None for webhook fires."""
    captured = {}

    @lightsei.on_trigger
    def handle():
        captured["kind"] = lightsei.trigger.kind
        captured["webhook_payload"] = lightsei.trigger.webhook_payload
        captured["scheduled_at"] = lightsei.trigger.scheduled_at
        return None

    body = {"channel": "#sales", "user": "ada"}
    result = trigmod._trigger_fire_bridge({
        "run_id": "run-xyz",
        "trigger_id": "trig-2",
        "trigger_kind": "webhook",
        "webhook_payload": body,
    })
    assert result["ok"] is True
    assert captured["kind"] == "webhook"
    assert captured["webhook_payload"] == body
    assert captured["scheduled_at"] is None


def test_bridge_returns_dict_payload_from_handler():
    @lightsei.on_trigger
    def handle():
        return {"items_processed": 3}

    result = trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t", "trigger_kind": "cron",
    })
    assert result == {"ok": True, "items_processed": 3}


def test_bridge_wraps_non_dict_return_in_value_key():
    @lightsei.on_trigger
    def handle():
        return "all good"

    result = trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t", "trigger_kind": "cron",
    })
    assert result == {"ok": True, "value": "all good"}


# ---------- Bridge: context lifecycle ---------- #


def test_context_cleared_after_bridge_returns():
    """After the bridge call exits, the accessor is back to manual.
    Crucial because the bot process is long-lived; a leaked context
    would mis-label every subsequent run."""
    @lightsei.on_trigger
    def handle():
        assert lightsei.trigger.kind == "cron"
        return None

    trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t", "trigger_kind": "cron",
    })

    assert lightsei.trigger.kind == "manual"
    assert lightsei.trigger.scheduled_at is None


def test_context_cleared_after_handler_raises():
    """Same guarantee as above, but for the exception path —
    contextvar token must reset even when the handler explodes."""
    @lightsei.on_trigger
    def handle():
        raise RuntimeError("boom")

    result = trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t", "trigger_kind": "cron",
    })
    assert result["ok"] is False
    assert "boom" in result["error"]

    assert lightsei.trigger.kind == "manual"


def test_run_id_cleared_after_bridge_returns():
    """Same lifecycle for the run_id context the bridge sets."""
    @lightsei.on_trigger
    def handle():
        assert get_run_id() == "run-abc"
        return None

    trigmod._trigger_fire_bridge({
        "run_id": "run-abc", "trigger_id": "t", "trigger_kind": "cron",
    })

    assert get_run_id() is None


# ---------- Bridge: error paths ---------- #


def test_bridge_returns_error_when_no_handler_registered():
    """No @on_trigger handler: the bridge returns a clean error
    rather than silently succeeding. The operator sees the wiring
    bug in the run's events."""
    # Fixture cleared _handler; don't register one.
    result = trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t", "trigger_kind": "cron",
    })
    assert result["ok"] is False
    assert "on_trigger" in result["error"]


def test_bridge_tolerates_malformed_scheduled_at():
    """A non-ISO scheduled_at becomes None in the accessor; the
    handler still runs."""
    captured = {}

    @lightsei.on_trigger
    def handle():
        captured["scheduled_at"] = lightsei.trigger.scheduled_at
        return None

    trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t", "trigger_kind": "cron",
        "scheduled_at": "not an iso date",
    })
    assert captured["scheduled_at"] is None


def test_bridge_defaults_kind_to_cron_when_payload_missing_it():
    """Defense-in-depth: a malformed payload missing trigger_kind
    falls back to 'cron' (the more common case) instead of breaking
    the handler with an unknown kind."""
    captured = {}

    @lightsei.on_trigger
    def handle():
        captured["kind"] = lightsei.trigger.kind
        return None

    trigmod._trigger_fire_bridge({
        "run_id": "r", "trigger_id": "t",
    })
    assert captured["kind"] == "cron"
