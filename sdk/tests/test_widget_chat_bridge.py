"""Phase 21.6: tests for the SDK-side widget.chat bridge.

The bridge is a built-in @on_command("widget.chat") that lives in
_chat.py. It takes a {conversation_id, user_message,
conversation_history} payload from the backend orchestrator,
calls the user-registered @on_chat("widget") handler, and posts
the response (or escalates) via the Phase 21.5 SDK helpers.

Covers:

- Bridge is auto-registered on import.
- String return → /widget-bot/respond.
- LightseiEscalate → /widget-bot/escalate with reason + payload.
- Uncaught exception → /widget-bot/escalate with reason='bot_crash'.
- No widget handler registered → escalate 'bot_unconfigured'.
- None return → no_reply (no HTTP call).
- Empty-string return → no_reply.
- Non-string non-None return → error, no HTTP call.
- Missing conversation_id in payload → error.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pytest

import lightsei
from lightsei import _chat
from lightsei._chat import _widget_chat_bridge
from lightsei._client import _client
from lightsei._commands import _handlers as _command_handlers


@pytest.fixture(autouse=True)
def _reset_state_between_tests():
    yield
    _client._reset_for_tests()
    _chat._handlers.clear()


@contextmanager
def bridge_fake(
    *,
    capabilities: Optional[list[str]] = None,
    captured: Optional[list[dict]] = None,
) -> Iterator[str]:
    """Fake backend that accepts /widget-bot/respond + /widget-bot/escalate
    and records the request bodies in `captured`."""
    state = {
        "capabilities": capabilities or [
            "widget:respond", "widget:escalate",
        ],
        "captured": captured,
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/agents/"):
                self._send_json(200, {
                    "name": "vega",
                    "capabilities": state["capabilities"],
                })
                return
            self.send_response(404); self.end_headers()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                req_body = json.loads(raw.decode() or "{}")
            except Exception:
                req_body = {}
            if self.path in ("/widget-bot/respond", "/widget-bot/escalate"):
                if state["captured"] is not None:
                    state["captured"].append({"path": self.path, "body": req_body})
                if self.path.endswith("/respond"):
                    self._send_json(200, {
                        "ok": True, "message_id": 1, "conversation_id":
                        req_body.get("conversation_id"),
                    })
                else:
                    self._send_json(200, {
                        "ok": True, "status": "escalated",
                        "escalation_id": "E_FAKE",
                    })
                return
            self._send_json(200, {"status": "ok"})

        def log_message(self, *a, **kw): pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown(); srv.server_close()


# ---------- Registration ---------- #


def test_bridge_is_auto_registered_on_import():
    """Importing lightsei (which imports _chat) registers the
    bridge under `widget.chat` in the command handler registry —
    no opt-in needed."""
    assert "widget.chat" in _command_handlers


# ---------- String return → /respond ---------- #


def test_bridge_string_return_posts_respond():
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            return f"echo: {turn['user_message']}"

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "hi",
            "conversation_history": [],
        })

    assert result == {"ok": True, "responded": True}
    assert len(captured) == 1
    call = captured[0]
    assert call["path"] == "/widget-bot/respond"
    assert call["body"]["conversation_id"] == "C_1"
    assert call["body"]["text"] == "echo: hi"


def test_bridge_passes_history_to_handler():
    captured: list[dict] = []
    seen_turn: dict = {}
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            seen_turn.update(turn)
            return "k"

        _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "current",
            "conversation_history": [
                {"role": "user", "text": "prior-user"},
                {"role": "bot", "text": "prior-bot"},
            ],
        })

    assert seen_turn["conversation_id"] == "C_1"
    assert seen_turn["user_message"] == "current"
    assert seen_turn["conversation_history"] == [
        {"role": "user", "text": "prior-user"},
        {"role": "bot", "text": "prior-bot"},
    ]


# ---------- LightseiEscalate → /escalate ---------- #


def test_bridge_raises_escalate_routes_to_escalate_endpoint():
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            raise lightsei.LightseiEscalate(
                "refund_request",
                payload={"hint": "user said refund"},
            )

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "give me a refund",
        })

    assert result["ok"] is True
    assert result["escalated"] is True
    assert result["reason"] == "refund_request"

    assert len(captured) == 1
    call = captured[0]
    assert call["path"] == "/widget-bot/escalate"
    assert call["body"]["reason"] == "refund_request"
    assert call["body"]["payload"] == {"hint": "user said refund"}


# ---------- Uncaught exception → bot_crash escalate ---------- #


def test_bridge_uncaught_exception_escalates_as_bot_crash():
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            raise ValueError("kaboom")

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "x",
        })

    assert result["ok"] is False
    assert result["escalated"] is True
    assert "kaboom" in result["error"]

    assert len(captured) == 1
    assert captured[0]["body"]["reason"] == "bot_crash"
    assert "kaboom" in captured[0]["body"]["payload"]["error"]


# ---------- No handler registered → bot_unconfigured ---------- #


def test_bridge_no_widget_handler_escalates_as_unconfigured():
    """The orchestrator's capability check passed (widget:respond
    granted) but the bot has no @on_chat('widget') handler. Bridge
    escalates so an operator sees it in /inbox rather than the
    conversation going silent."""
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        # Deliberately don't register a widget handler.

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "hi",
        })

    assert result["ok"] is False
    assert result["error"] == "no_widget_handler_registered"
    assert len(captured) == 1
    assert captured[0]["path"] == "/widget-bot/escalate"
    assert captured[0]["body"]["reason"] == "bot_unconfigured"


# ---------- None / empty return → no_reply ---------- #


def test_bridge_none_return_is_no_reply():
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            return None

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "x",
        })

    assert result == {"ok": True, "no_reply": True}
    assert captured == []


def test_bridge_empty_string_return_is_no_reply():
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            return "   "

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "x",
        })

    assert result == {"ok": True, "no_reply": True}
    assert captured == []


# ---------- Bad return type → error, no HTTP ---------- #


def test_bridge_non_string_non_none_return_is_error():
    captured: list[dict] = []
    with bridge_fake(captured=captured) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

        @lightsei.on_chat("widget")
        def handler(turn):
            return {"oops": "returned a dict"}

        result = _widget_chat_bridge({
            "conversation_id": "C_1",
            "user_message": "x",
        })

    assert result["ok"] is False
    assert "must return a string or None" in result["error"]
    assert captured == []  # no HTTP call made


# ---------- Missing payload field ---------- #


def test_bridge_missing_conversation_id_returns_error():
    result = _widget_chat_bridge({"user_message": "x"})
    assert result["ok"] is False
    assert "conversation_id" in result["error"]
