"""Phase 20.7: SDK connector wrapper tests.

Three surfaces:

1. _invoke helper — capability gate, source_agent resolution, body
   shape, response unwrapping (strips the {ok, result} envelope).
2. Error mapping — backend's 403 capability_missing /
   connector_zone_mismatch land as typed LightseiCapabilityError /
   LightseiConnectorZoneError; other 4xx/5xx become LightseiError.
3. Per-connector wrappers — exercise one tool per connector
   end-to-end to confirm the package re-export + typed signatures
   actually thread through.

Uses a lightweight HTTP fake (similar to test_basic.fake_backend but
tailored to the /connectors/{type}/{tool} endpoint shape) so tests
never hit Google or the real backend.
"""
from __future__ import annotations

import base64
import http.server
import json
import socket
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pytest

import lightsei
from lightsei import _capabilities
from lightsei._client import _client
from lightsei.errors import (
    LightseiCapabilityError,
    LightseiConnectorZoneError,
    LightseiError,
)


@pytest.fixture(autouse=True)
def _reset_client_between_tests():
    yield
    _client._reset_for_tests()


# ---------- Connector HTTP fake ---------- #


@contextmanager
def connector_fake(
    *,
    capabilities: Optional[list[str]] = None,
    response_status: int = 200,
    response_body: Any = None,
    captured: Optional[list[dict]] = None,
) -> Iterator[str]:
    """Spin up an HTTP server that:

    - GET /agents/{name} → 200 with the capabilities the test asked
      for (so init() populates the local cache).
    - POST /connectors/{type}/{tool} → records the request body +
      returns the configured `response_status` / `response_body`.
    - POST /events, /heartbeats etc. → 200 (so the SDK doesn't
      complain).

    captured (if provided) gets one dict per /connectors POST with
    keys: connector_type, tool_name, body.
    """
    if response_body is None:
        response_body = {"ok": True, "result": {}}

    state = {
        "capabilities": capabilities,
        "response_status": response_status,
        "response_body": response_body,
        "captured": captured,
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/agents/"):
                # /agents/{name} or /agents/{name}/capabilities — both
                # served by the same fixed body.
                body = json.dumps({
                    "name": "test-agent",
                    "capabilities": state["capabilities"] or [],
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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

            if self.path.startswith("/connectors/"):
                # Path is /connectors/{type}/{tool}.
                parts = self.path.split("/")
                connector_type = parts[2] if len(parts) > 2 else ""
                tool_name = parts[3] if len(parts) > 3 else ""
                if state["captured"] is not None:
                    state["captured"].append({
                        "connector_type": connector_type,
                        "tool_name": tool_name,
                        "body": req_body,
                    })
                body = json.dumps(state["response_body"]).encode()
                self.send_response(state["response_status"])
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # /events, /heartbeats, anything else — bland 200.
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


# ---------- LightseiConnectorZoneError shape ---------- #


def test_connector_zone_error_carries_metadata():
    err = LightseiConnectorZoneError(
        connector_type="gmail",
        agent_name="researcher",
        agent_sensitivity_level="public",
        declared_zones=["internal", "sensitive", "pii"],
    )
    assert err.connector_type == "gmail"
    assert err.agent_name == "researcher"
    assert err.agent_sensitivity_level == "public"
    assert err.declared_zones == ["internal", "sensitive", "pii"]
    msg = str(err)
    assert "gmail" in msg
    assert "public" in msg


def test_connector_zone_error_is_a_lightsei_error():
    """User code that only catches LightseiError still catches this."""
    err = LightseiConnectorZoneError(
        connector_type="gmail", agent_name=None,
        agent_sensitivity_level=None,
    )
    assert isinstance(err, LightseiError)


# ---------- _invoke: capability gate ---------- #


def test_invoke_refuses_when_capability_not_granted():
    """Local cache says no `connector:gmail` → LightseiCapabilityError
    raised BEFORE any HTTP call. Saves a backend round-trip."""
    captured: list[dict] = []
    with connector_fake(
        capabilities=["internet"],  # no connector:gmail
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            lightsei.gmail.list_labels()
        assert exc.value.capability == "connector:gmail"
    # No /connectors POST was made.
    assert captured == []


def test_invoke_passes_when_capability_granted():
    """With the capability in the local cache, the call goes through
    + the request body has the expected shape."""
    captured: list[dict] = []
    with connector_fake(
        capabilities=["connector:gmail"],
        response_body={"ok": True, "result": {"labels": [{"id": "INBOX"}]}},
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        result = lightsei.gmail.list_labels()
        assert result == {"labels": [{"id": "INBOX"}]}

    assert len(captured) == 1
    call = captured[0]
    assert call["connector_type"] == "gmail"
    assert call["tool_name"] == "list_labels"
    # Resolved source_agent from lightsei.init().
    assert call["body"]["source_agent"] == "vega"
    assert call["body"]["payload"] == {}


# ---------- _invoke: source_agent resolution ---------- #


def test_invoke_uses_explicit_source_agent_over_init():
    """source_agent=... kwarg wins over agent_name from init()."""
    captured: list[dict] = []
    with connector_fake(
        capabilities=["connector:gmail"], captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        lightsei.gmail.list_labels(source_agent="explicit-name")
    assert captured[0]["body"]["source_agent"] == "explicit-name"


def test_invoke_raises_when_no_source_agent_anywhere():
    """init() without agent_name + no kwarg → LightseiError (not
    LightseiCapabilityError — the gate fails open before init
    completes the capability fetch, so we have to wait until the
    source-agent check to fail)."""
    with connector_fake(capabilities=["connector:gmail"]) as url:
        # init() with no agent_name. The capability check fails open
        # because the cache never loads (the agent-fetch needs a
        # name). Then the source_agent check trips.
        lightsei.init(api_key="k", agent_name=None, base_url=url)
        with pytest.raises(LightseiError) as exc:
            lightsei.gmail.list_labels()
        assert "source_agent" in str(exc.value)


# ---------- _invoke: response unwrapping ---------- #


def test_invoke_unwraps_result_envelope():
    """Backend returns {ok: True, result: <whatever>}. Wrapper hands
    bot code just the <whatever>."""
    with connector_fake(
        capabilities=["connector:gmail"],
        response_body={"ok": True, "result": {"messages": [1, 2, 3]}},
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        out = lightsei.gmail.search_inbox("is:unread")
        assert out == {"messages": [1, 2, 3]}


def test_invoke_returns_full_body_when_no_result_field():
    """Defensive: if the backend ever changes the envelope shape,
    return the whole body rather than crashing on missing 'result'."""
    with connector_fake(
        capabilities=["connector:gmail"],
        response_body={"surprise": "envelope"},
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        out = lightsei.gmail.list_labels()
        assert out == {"surprise": "envelope"}


# ---------- _invoke: error mapping ---------- #


def test_invoke_maps_403_capability_missing_to_typed_error():
    """Stale local cache → backend's 403 capability_missing still
    surfaces as LightseiCapabilityError with the right attributes."""
    body = {
        "detail": {
            "error": "capability_missing",
            "capability": "connector:gmail",
            "agent_name": "vega",
            "granted": ["internet"],
            "message": "agent 'vega' does not have...",
        },
    }
    with connector_fake(
        # Local cache grants the capability — server's truth is no.
        capabilities=["connector:gmail"],
        response_status=403,
        response_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            lightsei.gmail.list_labels()
        assert exc.value.capability == "connector:gmail"
        assert exc.value.granted == ["internet"]
        assert exc.value.agent_name == "vega"


def test_invoke_maps_403_zone_mismatch_to_typed_error():
    """Backend's 403 connector_zone_mismatch → LightseiConnectorZoneError."""
    body = {
        "detail": {
            "error": "connector_zone_mismatch",
            "connector_type": "gmail",
            "agent_name": "researcher",
            "agent_sensitivity_level": "public",
            "declared_zones": ["internal", "sensitive", "pii"],
            "message": "connector 'gmail' refuses calls from 'public'...",
        },
    }
    with connector_fake(
        capabilities=["connector:gmail"],
        response_status=403,
        response_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="researcher", base_url=url)
        with pytest.raises(LightseiConnectorZoneError) as exc:
            lightsei.gmail.list_labels()
        assert exc.value.connector_type == "gmail"
        assert exc.value.agent_sensitivity_level == "public"
        assert "internal" in exc.value.declared_zones


def test_invoke_maps_400_not_installed_to_lightsei_error():
    """400 connector_not_installed → LightseiError (not a typed
    subclass; user code that wants the specific shape can string-
    check `error` in the message)."""
    body = {
        "detail": {
            "error": "connector_not_installed",
            "connector_type": "gmail",
            "message": "no active 'gmail' install for this workspace.",
        },
    }
    with connector_fake(
        capabilities=["connector:gmail"],
        response_status=400, response_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiError) as exc:
            lightsei.gmail.list_labels()
        # Not the typed subclasses.
        assert not isinstance(exc.value, LightseiCapabilityError)
        assert not isinstance(exc.value, LightseiConnectorZoneError)
        assert "connector_not_installed" in str(exc.value)


def test_invoke_maps_502_call_failed_to_lightsei_error():
    """502 connector_call_failed → LightseiError carrying the
    upstream status in the message."""
    body = {
        "detail": {
            "error": "connector_call_failed",
            "message": "upstream gmail API call failed",
            "_debug": {"upstream_status": 503, "error": "Service Unavailable"},
        },
    }
    with connector_fake(
        capabilities=["connector:gmail"],
        response_status=502, response_body=body,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        with pytest.raises(LightseiError) as exc:
            lightsei.gmail.list_labels()
        assert "connector_call_failed" in str(exc.value)


# ---------- Per-connector wrappers (one tool each) ---------- #


def test_gmail_send_email_threads_payload():
    captured: list[dict] = []
    with connector_fake(
        capabilities=["connector:gmail"],
        response_body={"ok": True, "result": {"id": "M1", "thread_id": "T1"}},
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)
        out = lightsei.gmail.send_email(
            to="alice@example.com",
            subject="hi",
            body="hello",
            cc=["bob@example.com"],
        )
        assert out == {"id": "M1", "thread_id": "T1"}

    call = captured[0]
    assert call["tool_name"] == "send_email"
    assert call["body"]["payload"] == {
        "to": "alice@example.com",
        "subject": "hi",
        "body": "hello",
        "cc": ["bob@example.com"],
    }


def test_calendar_list_events_omits_none_kwargs():
    """Optional kwargs that are None shouldn't bloat the payload —
    keeps the backend's request_payload small and clean for
    debugging."""
    captured: list[dict] = []
    with connector_fake(
        capabilities=["connector:google_calendar"],
        response_body={"ok": True, "result": {"events": []}},
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="scheduler", base_url=url)
        lightsei.calendar.list_events(time_min="2026-05-20T00:00:00Z")

    payload = captured[0]["body"]["payload"]
    # Only the explicitly-passed kwargs plus the defaults that have
    # non-None values land in the payload.
    assert "time_min" in payload
    assert "calendar_id" not in payload
    assert "time_max" not in payload
    assert "query" not in payload
    assert payload["max_results"] == 25  # default
    assert payload["single_events"] is True


def test_drive_list_files_threads_query():
    captured: list[dict] = []
    with connector_fake(
        capabilities=["connector:google_drive"],
        response_body={"ok": True, "result": {"files": []}},
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="archivist", base_url=url)
        lightsei.drive.list_files(
            query="name contains 'budget'", page_size=10,
        )
    payload = captured[0]["body"]["payload"]
    assert payload["query"] == "name contains 'budget'"
    assert payload["page_size"] == 10


def test_drive_download_file_bytes_decodes_base64():
    """download_file_bytes is a convenience: returns raw bytes +
    mime + name without the bot having to base64-decode."""
    raw_content = b"Hello, world!\n"
    response = {
        "ok": True,
        "result": {
            "file_id": "F_1",
            "name": "greeting.txt",
            "source_mime_type": "text/plain",
            "mime_type": "text/plain",
            "size": len(raw_content),
            "content_b64": base64.b64encode(raw_content).decode("ascii"),
        },
    }
    with connector_fake(
        capabilities=["connector:google_drive"],
        response_body=response,
    ) as url:
        lightsei.init(api_key="k", agent_name="archivist", base_url=url)
        content, mime, name = lightsei.drive.download_file_bytes("F_1")
        assert content == raw_content
        assert mime == "text/plain"
        assert name == "greeting.txt"


def test_drive_upload_file_bytes_encodes_base64():
    """Mirror of download_file_bytes: bot passes raw bytes, wrapper
    base64-encodes before sending."""
    captured: list[dict] = []
    with connector_fake(
        capabilities=["connector:google_drive"],
        response_body={"ok": True, "result": {"id": "NEW_F"}},
        captured=captured,
    ) as url:
        lightsei.init(api_key="k", agent_name="archivist", base_url=url)
        lightsei.drive.upload_file_bytes(
            name="report.txt",
            content=b"Q3 results...",
            mime_type="text/plain",
        )

    payload = captured[0]["body"]["payload"]
    assert payload["name"] == "report.txt"
    assert payload["mime_type"] == "text/plain"
    assert base64.b64decode(payload["content_b64"]) == b"Q3 results..."


# ---------- Transport failure ---------- #


def test_invoke_transport_error_raises_lightsei_error():
    """If the SDK can't reach the backend at all, surface as
    LightseiError (not a typed subclass). Graceful degradation —
    bot code that catches LightseiError keeps running rather than
    crashing on a network blip."""
    # init() against a working fake to populate the capability cache,
    # then redirect the HTTP client to a connect-refused address.
    with connector_fake(capabilities=["connector:gmail"]) as url:
        lightsei.init(api_key="k", agent_name="vega", base_url=url)

    # Now the fake is gone (context exited). Re-point _http to a
    # closed port so the next call fails at the transport layer.
    import httpx
    _client._http = httpx.Client(
        base_url="http://127.0.0.1:1",  # always-refused
        timeout=0.5,
    )

    with pytest.raises(LightseiError) as exc:
        lightsei.gmail.list_labels()
    assert "transport" in str(exc.value).lower()


# ---------- Pre-init guard ---------- #


def test_invoke_before_init_raises():
    """Calling lightsei.gmail.* without lightsei.init() → clean
    LightseiError (not an AttributeError on _http=None)."""
    # _client is in its reset/initial state from the autouse fixture.
    assert _client._http is None
    with pytest.raises(LightseiError) as exc:
        lightsei.gmail.list_labels()
    assert "init()" in str(exc.value)
