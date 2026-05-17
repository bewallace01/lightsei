"""Phase 16.4: SDK tests for cross-zone dispatch refusal surfacing.

The backend gate is tested in backend/tests/test_cross_zone_dispatch.py.
This file covers the SDK side:

1. LightseiCrossZoneError attributes + helpful message.
2. send_command catches the backend's 403 cross_zone_blocked and
   re-raises as LightseiCrossZoneError (NOT a generic LightseiError).
3. Non-cross-zone 403s still fall through as LightseiError so the
   typed exception is unambiguous.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

import lightsei
from lightsei import _client
from lightsei.errors import LightseiCrossZoneError, LightseiError


@pytest.fixture(autouse=True)
def _reset_client_between_tests():
    yield
    _client._reset_for_tests()


# ---------- Fake backend ---------- #


@contextmanager
def cross_zone_backend(
    *,
    response_status: int = 403,
    response_body: dict | None = None,
    capabilities: list[str] | None = None,
) -> Iterator[str]:
    """Minimal backend that returns a configured response to a
    send_command POST. Default = 403 cross_zone_blocked. Also serves
    GET /agents/{name} so init() succeeds, and a heartbeat handler
    that includes the capability list for the gate."""
    if capabilities is None:
        capabilities = ["send_command"]

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/agents/"):
                name = self.path[len("/agents/"):]
                body = json.dumps({
                    "name": name,
                    "system_prompt": None,
                    "sensitivity_level": "pii",
                    "capabilities": list(capabilities),
                    "dispatches_cross_zone": False,
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0") or "0")
            _ = self.rfile.read(length) if length else b""
            if self.path.endswith("/instances/heartbeat"):
                body = json.dumps({
                    "status": "active",
                    "capabilities": list(capabilities),
                    "sensitivity_level": "pii",
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/agents/") and self.path.endswith("/commands"):
                payload = response_body if response_body is not None else {
                    "detail": {
                        "error": "cross_zone_blocked",
                        "source_agent": "src",
                        "source_zone": "pii",
                        "target_agent": self.path.split("/")[2],
                        "target_zone": "public",
                        "message": "cross zone blocked test",
                    }
                }
                raw = json.dumps(payload).encode()
                self.send_response(response_status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_error(404)

        def log_message(self, *a, **kw):
            return

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- LightseiCrossZoneError ---------- #


def test_error_carries_zone_metadata():
    err = LightseiCrossZoneError(
        source_agent="crm",
        source_zone="pii",
        target_agent="research",
        target_zone="public",
    )
    assert err.source_agent == "crm"
    assert err.source_zone == "pii"
    assert err.target_agent == "research"
    assert err.target_zone == "public"
    msg = str(err)
    assert "'crm'" in msg
    assert "'pii'" in msg
    assert "'research'" in msg
    assert "'public'" in msg
    assert "dispatches_cross_zone" in msg


def test_error_uses_explicit_message_when_provided():
    err = LightseiCrossZoneError(
        source_agent="a", source_zone="pii",
        target_agent="b", target_zone="public",
        message="custom from server",
    )
    assert "custom from server" in str(err)


def test_error_is_a_lightsei_error_subclass():
    """LightseiCrossZoneError subclasses LightseiError so existing
    code that catches the base type still works."""
    err = LightseiCrossZoneError(
        source_agent="a", source_zone="pii",
        target_agent="b", target_zone="public",
    )
    assert isinstance(err, LightseiError)


# ---------- send_command surfacing ---------- #


def test_send_command_raises_cross_zone_error_on_403(client_unused=None):
    """The canonical case: backend returns 403 with the
    cross_zone_blocked error code, SDK re-raises as the typed
    LightseiCrossZoneError so user code can catch it specifically."""
    with cross_zone_backend() as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url)
        with pytest.raises(LightseiCrossZoneError) as exc:
            lightsei.send_command("research", "research.scan", {})
        assert exc.value.source_zone == "pii"
        assert exc.value.target_zone == "public"
        assert exc.value.target_agent == "research"


def test_send_command_other_403_falls_through_as_generic_error():
    """A 403 that isn't a cross_zone_blocked code (e.g. some future
    permission check) should NOT be misreported as a cross-zone
    error. Falls through to the generic LightseiError so the typed
    exception stays unambiguous."""
    other_403 = {"detail": "permission denied for some other reason"}
    with cross_zone_backend(
        response_status=403, response_body=other_403,
    ) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url)
        with pytest.raises(LightseiError) as exc:
            lightsei.send_command("research", "research.scan", {})
        # Generic LightseiError, NOT the cross-zone subclass.
        assert not isinstance(exc.value, LightseiCrossZoneError)


def test_send_command_403_with_malformed_body_falls_through():
    """If the backend returns a 403 but the body isn't parseable JSON,
    fall through to the generic error rather than crashing on the
    detail-extraction path."""
    with cross_zone_backend(
        response_status=403,
        response_body={"not_a_dict": True},  # missing detail.error
    ) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url)
        with pytest.raises(LightseiError) as exc:
            lightsei.send_command("research", "research.scan", {})
        assert not isinstance(exc.value, LightseiCrossZoneError)
