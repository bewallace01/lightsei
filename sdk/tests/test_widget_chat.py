"""Phase 21.5: SDK tests for the widget chat surface.

Three surfaces:

1. @on_chat decorator — multi-channel registry. Default form
   (`@on_chat`) and channel form (`@on_chat("widget")`) both work
   without colliding.
2. LightseiEscalate exception shape.
3. lightsei.respond / lightsei.escalate helpers — capability gate,
   source_agent resolution, body shape, error mapping.

Uses the test_basic.fake_backend helper extended with handlers
for the two new /widget-bot/* endpoints.
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
from lightsei._client import _client
from lightsei.errors import (
    LightseiCapabilityError,
    LightseiError,
    LightseiEscalate,
)


@pytest.fixture(autouse=True)
def _reset_client_between_tests():
    yield
    _client._reset_for_tests()
    # Clear the chat registry too — handlers persist at module level
    # so state leaks across tests without this.
    _chat._handlers.clear()


# ---------- Fake backend for the two new endpoints ---------- #


@contextmanager
def widget_bot_fake(
    *,
    capabilities: Optional[list[str]] = None,
    respond_status: int = 200,
    respond_body: Any = None,
    escalate_status: int = 200,
    escalate_body: Any = None,
    captured: Optional[list[dict]] = None,
) -> Iterator[str]:
    """Spin up an HTTP server that handles:

    - GET /agents/{name} → capability list for SDK init.
    - POST /widget-bot/respond → configurable status/body, capture POSTed
      body if `captured` is given.
    - POST /widget-bot/escalate → same shape.
    - POST /events, /heartbeats → 200 (SDK keep-alive).
    """
    if respond_body is None:
        respond_body = {"ok": True, "message_id": 42, "conversation_id": "C_1"}
    if escalate_body is None:
        escalate_body = {"ok": True, "status": "escalated", "escalation_id": "E_1"}

    state = {
        "capabilities": capabilities or [],
        "respond_status": respond_status,
        "respond_body": respond_body,
        "escalate_status": escalate_status,
        "escalate_body": escalate_body,
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
                    "name": "test-bot",
                    "capabilities": state["capabilities"],
                })
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                req_body = json.loads(raw.decode() or "{}")
            except Exception:
                req_body = {}

            if self.path == "/widget-bot/respond":
                if state["captured"] is not None:
                    state["captured"].append({
                        "path": self.path, "body": req_body,
                    })
                self._send_json(state["respond_status"], state["respond_body"])
                return
            if self.path == "/widget-bot/escalate":
                if state["captured"] is not None:
                    state["captured"].append({
                        "path": self.path, "body": req_body,
                    })
                self._send_json(state["escalate_status"], state["escalate_body"])
                return
            # /events, /heartbeats, etc.
            self._send_json(200, {"status": "ok"})

        def log_message(self, *a, **kw):
            pass

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
        srv.shutdown()
        srv.server_close()


# ---------- @on_chat decorator ---------- #


def test_on_chat_default_form_no_parens():
    """`@lightsei.on_chat` (no parens) registers under the None
    channel — backward-compatible with pre-21.5 callers."""
    @lightsei.on_chat
    def handler(msgs):
        return "default-reply"

    assert _chat.has_chat_handler()
    assert _chat.has_chat_handler(None)
    assert not _chat.has_chat_handler("widget")
    assert _chat.get_chat_handler()("anything") == "default-reply"


def test_on_chat_channel_form():
    """`@lightsei.on_chat("widget")` registers under the named
    channel."""
    @lightsei.on_chat("widget")
    def handler(turn):
        return "widget-reply"

    assert _chat.has_chat_handler("widget")
    assert not _chat.has_chat_handler()
    assert _chat.get_chat_handler("widget")({}) == "widget-reply"


def test_on_chat_both_forms_coexist():
    """A bot can register both a default handler AND a widget
    handler simultaneously; they live under different keys."""
    @lightsei.on_chat
    def default_handler(msgs):
        return "default"

    @lightsei.on_chat("widget")
    def widget_handler(turn):
        return "widget"

    assert _chat.has_chat_handler()
    assert _chat.has_chat_handler("widget")
    assert _chat.get_chat_handler()("x") == "default"
    assert _chat.get_chat_handler("widget")({}) == "widget"


def test_on_chat_redecorate_replaces():
    """Re-decorating with the same channel replaces the prior
    handler — useful in dev/hot-reload paths."""
    @lightsei.on_chat("widget")
    def first(turn): return "first"

    @lightsei.on_chat("widget")
    def second(turn): return "second"

    assert _chat.get_chat_handler("widget")({}) == "second"


def test_get_chat_handler_returns_none_for_missing_channel():
    assert _chat.get_chat_handler("nonexistent") is None


# ---------- LightseiEscalate ---------- #


def test_lightsei_escalate_carries_reason_and_payload():
    err = lightsei.LightseiEscalate(
        "refund_request", payload={"last_user_message": "refund?"},
    )
    assert err.reason == "refund_request"
    assert err.payload == {"last_user_message": "refund?"}
    assert "refund_request" in str(err)


def test_lightsei_escalate_default_payload_is_empty_dict():
    err = lightsei.LightseiEscalate("boom")
    assert err.payload == {}


def test_lightsei_escalate_is_not_a_lightsei_error():
    """LightseiEscalate is a control-flow signal, not an error.
    User code that catches LightseiError must not swallow it."""
    err = lightsei.LightseiEscalate("x")
    assert not isinstance(err, LightseiError)


# ---------- lightsei.respond ---------- #


def test_respond_happy_path():
    captured: list[dict] = []
    with widget_bot_fake(
        capabilities=["widget:respond"],
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        out = lightsei.respond("C_xyz", "hi there")
        assert out == {"ok": True, "message_id": 42, "conversation_id": "C_1"}

    call = captured[0]
    assert call["path"] == "/widget-bot/respond"
    assert call["body"] == {
        "source_agent": "vega",
        "conversation_id": "C_xyz",
        "text": "hi there",
    }


def test_respond_refuses_without_capability():
    """Local capability cache says no → LightseiCapabilityError
    raised BEFORE any HTTP call."""
    captured: list[dict] = []
    with widget_bot_fake(
        capabilities=["internet"],  # no widget:respond
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            lightsei.respond("C_xyz", "hi")
        assert exc.value.capability == "widget:respond"
    assert captured == []


def test_respond_maps_403_to_typed_error():
    """Stale local cache → backend's 403 surfaces as
    LightseiCapabilityError with the right attributes."""
    body = {
        "detail": {
            "error": "capability_missing",
            "capability": "widget:respond",
            "agent_name": "vega",
            "granted": ["internet"],
        },
    }
    with widget_bot_fake(
        capabilities=["widget:respond"],  # local says yes
        respond_status=403,
        respond_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            lightsei.respond("C_xyz", "hi")
        assert exc.value.granted == ["internet"]


def test_respond_409_resolved_surfaces_as_lightsei_error():
    """409 conversation_resolved → plain LightseiError with the
    code in the message."""
    body = {
        "detail": {
            "error": "conversation_resolved",
            "message": "conversation was marked resolved",
        },
    }
    with widget_bot_fake(
        capabilities=["widget:respond"],
        respond_status=409, respond_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiError) as exc:
            lightsei.respond("C_xyz", "hi")
        assert "409" in str(exc.value)


def test_respond_explicit_source_agent_overrides_init():
    captured: list[dict] = []
    with widget_bot_fake(
        capabilities=["widget:respond"], captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        lightsei.respond("C_xyz", "hi", source_agent="explicit")
    assert captured[0]["body"]["source_agent"] == "explicit"


def test_respond_requires_args():
    with widget_bot_fake(capabilities=["widget:respond"]) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(ValueError):
            lightsei.respond("", "hi")
        with pytest.raises(ValueError):
            lightsei.respond("C", "")


def test_respond_before_init_raises_clean_error():
    """Calling respond() without init() → clean LightseiError,
    not AttributeError on _http=None."""
    assert _client._http is None
    with pytest.raises(LightseiError) as exc:
        lightsei.respond("C", "hi")
    assert "init()" in str(exc.value)


# ---------- lightsei.escalate ---------- #


def test_escalate_happy_path():
    captured: list[dict] = []
    with widget_bot_fake(
        capabilities=["widget:escalate"], captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        out = lightsei.escalate("C_xyz", "refund_request")
        assert out["ok"] is True
        assert out["status"] == "escalated"

    call = captured[0]
    assert call["path"] == "/widget-bot/escalate"
    assert call["body"]["reason"] == "refund_request"
    assert call["body"]["payload"] == {}


def test_escalate_passes_payload():
    captured: list[dict] = []
    with widget_bot_fake(
        capabilities=["widget:escalate"], captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        lightsei.escalate(
            "C_xyz", "boom",
            payload={"last_user_message": "wtf"},
        )
    assert captured[0]["body"]["payload"] == {"last_user_message": "wtf"}


def test_escalate_refuses_without_capability():
    captured: list[dict] = []
    with widget_bot_fake(
        capabilities=["widget:respond"],  # no widget:escalate
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            lightsei.escalate("C_xyz", "boom")
        assert exc.value.capability == "widget:escalate"
    assert captured == []


def test_escalate_maps_403_to_typed_error():
    body = {
        "detail": {
            "error": "capability_missing",
            "capability": "widget:escalate",
            "agent_name": "vega",
            "granted": ["widget:respond"],
        },
    }
    with widget_bot_fake(
        capabilities=["widget:escalate"],
        escalate_status=403, escalate_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiCapabilityError):
            lightsei.escalate("C_xyz", "boom")


def test_escalate_requires_args():
    with widget_bot_fake(capabilities=["widget:escalate"]) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(ValueError):
            lightsei.escalate("", "boom")
        with pytest.raises(ValueError):
            lightsei.escalate("C", "")
