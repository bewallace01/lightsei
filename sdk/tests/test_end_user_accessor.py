"""Phase 25.5: tests for lightsei.end_user accessor + PII redaction.

Three surfaces:

1. The accessor reports anonymous outside a widget.chat dispatch
   and the right values inside one. Tested by driving the bridge
   directly with hand-crafted payloads.
2. PII redaction: `email` returns the raw address only when the
   bot's `sensitivity_hint` is 'pii'. Public / internal / sensitive
   zones get None even when an end user IS identified.
3. The widget.chat bridge sets + resets the contextvar around the
   handler call so a long-lived bot process doesn't bleed identity
   between turns.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

import lightsei
from lightsei import _chat, _end_user
from lightsei._chat import _widget_chat_bridge
from lightsei._end_user import (
    _EndUserContext,
    _reset_end_user_context,
    _set_end_user_context,
)


@pytest.fixture(autouse=True)
def _reset_handlers():
    """Each test wires its own @on_chat('widget') handler; clear the
    registry between cases so they don't bleed."""
    yield
    _chat._handlers.clear()


@contextmanager
def end_user_context(
    *, id: str = "eu-1", email: str = "a@example.com",
    display_name: str = "Alice", sensitivity_hint: str = "pii",
) -> Iterator[None]:
    """Helper: set the end_user contextvar for the duration of the
    with-block. Lets us test the accessor in isolation from the
    bridge plumbing."""
    token = _set_end_user_context(_EndUserContext(
        id=id, email=email, display_name=display_name,
        sensitivity_hint=sensitivity_hint,
    ))
    try:
        yield
    finally:
        _reset_end_user_context(token)


# ---------- Accessor outside any dispatch ---------- #


def test_accessor_anonymous_outside_dispatch():
    """No context set = anonymous. Bots running outside a widget.chat
    handler (CLI smoke, dashboard 'Run now', etc) see this."""
    assert lightsei.end_user.is_identified is False
    assert lightsei.end_user.id is None
    assert lightsei.end_user.email is None
    assert lightsei.end_user.display_name is None


# ---------- Accessor inside dispatch ---------- #


def test_accessor_identified_pii_zone():
    with end_user_context(
        id="eu-pii", email="pii@example.com",
        display_name="Alice", sensitivity_hint="pii",
    ):
        assert lightsei.end_user.is_identified is True
        assert lightsei.end_user.id == "eu-pii"
        assert lightsei.end_user.email == "pii@example.com"
        assert lightsei.end_user.display_name == "Alice"


@pytest.mark.parametrize("zone", ["public", "internal", "sensitive"])
def test_accessor_redacts_email_outside_pii_zone(zone: str):
    """The whole point of the redaction: a bot in any non-PII zone
    that does `lightsei.end_user.email` gets None even when an end
    user IS identified. id + display_name pass through."""
    with end_user_context(
        id="eu-x", email="leaky@example.com",
        display_name="Bob", sensitivity_hint=zone,
    ):
        assert lightsei.end_user.is_identified is True
        assert lightsei.end_user.id == "eu-x"
        assert lightsei.end_user.email is None  # redacted
        assert lightsei.end_user.display_name == "Bob"


def test_accessor_resets_cleanly_after_with_block():
    """Context manager pattern works; accessor flips back to
    anonymous after the with-block exits."""
    with end_user_context():
        assert lightsei.end_user.is_identified is True
    assert lightsei.end_user.is_identified is False


# ---------- Bridge plumbing ---------- #


def test_bridge_sets_end_user_context_around_handler():
    """A widget.chat payload with `end_user` populates the accessor
    while the handler runs. The handler captures the values; the
    bridge resets the contextvar before returning."""
    seen: dict = {}

    @lightsei.on_chat("widget")
    def handle(turn):
        seen["is_identified"] = lightsei.end_user.is_identified
        seen["id"] = lightsei.end_user.id
        seen["email"] = lightsei.end_user.email
        seen["display_name"] = lightsei.end_user.display_name
        return None  # no reply so we don't need a fake backend

    r = _widget_chat_bridge({
        "conversation_id": "C_1",
        "user_message": "hi",
        "conversation_history": [],
        "end_user": {
            "id": "eu-bridge",
            "email": "bridge@example.com",
            "display_name": "Carol",
            "sensitivity_hint": "pii",
        },
    })

    assert r == {"ok": True, "no_reply": True}
    assert seen == {
        "is_identified": True,
        "id": "eu-bridge",
        "email": "bridge@example.com",
        "display_name": "Carol",
    }
    # After bridge returns, contextvar is reset.
    assert lightsei.end_user.is_identified is False


def test_bridge_skips_end_user_context_when_payload_absent():
    """Anonymous widget conversations don't include `end_user` on the
    payload; the bridge leaves the accessor in anonymous state."""
    seen: dict = {}

    @lightsei.on_chat("widget")
    def handle(turn):
        seen["is_identified"] = lightsei.end_user.is_identified
        return None

    _widget_chat_bridge({
        "conversation_id": "C_2",
        "user_message": "anon-msg",
        "conversation_history": [],
    })

    assert seen["is_identified"] is False
    assert lightsei.end_user.is_identified is False


def test_bridge_redacts_email_when_sensitivity_hint_is_public():
    """Backend orchestrator passes the BOT's sensitivity_level as
    sensitivity_hint. A public-zone bot with an identified end user
    sees the id + display_name but the email is redacted."""
    seen: dict = {}

    @lightsei.on_chat("widget")
    def handle(turn):
        seen["id"] = lightsei.end_user.id
        seen["email"] = lightsei.end_user.email
        seen["display_name"] = lightsei.end_user.display_name
        return None

    _widget_chat_bridge({
        "conversation_id": "C_3",
        "user_message": "hi",
        "conversation_history": [],
        "end_user": {
            "id": "eu-public",
            "email": "should-not-leak@example.com",
            "display_name": "Dana",
            "sensitivity_hint": "public",
        },
    })

    assert seen == {
        "id": "eu-public",
        "email": None,  # redacted because the bot is in 'public' zone
        "display_name": "Dana",
    }


def test_bridge_resets_context_even_when_handler_raises():
    """Crash inside the handler must not leak end-user context into
    the next turn. The bridge's try/finally guarantees this."""

    @lightsei.on_chat("widget")
    def handle(turn):
        assert lightsei.end_user.is_identified  # was set
        raise RuntimeError("oh no")

    _widget_chat_bridge({
        "conversation_id": "C_4",
        "user_message": "hi",
        "conversation_history": [],
        "end_user": {
            "id": "eu-crash",
            "email": "x@example.com",
            "display_name": "Erin",
            "sensitivity_hint": "pii",
        },
    })

    # After the bridge handles the crash, contextvar is back to
    # anonymous so the next turn sees a clean slate.
    assert lightsei.end_user.is_identified is False


def test_bridge_ignores_end_user_payload_without_id():
    """Defensive: a malformed end_user dict missing `id` is treated
    as if it weren't there (anonymous). Lets the backend evolve the
    field set without forcing every SDK upgrade to lockstep."""
    seen: dict = {}

    @lightsei.on_chat("widget")
    def handle(turn):
        seen["is_identified"] = lightsei.end_user.is_identified
        return None

    _widget_chat_bridge({
        "conversation_id": "C_5",
        "user_message": "hi",
        "conversation_history": [],
        "end_user": {"email": "noid@example.com"},  # missing id
    })

    assert seen["is_identified"] is False
