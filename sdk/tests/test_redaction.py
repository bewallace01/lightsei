"""Phase 16.5: SDK redaction primitives + handoff_span tests.

Four surfaces:

1. Built-in detectors (email / phone / SSN / Luhn-valid card).
2. `register_redactor` for custom detectors + name-collision
   semantics.
3. `redact_payload` recursive over dict / list / tuple containers.
4. Auto-apply on `lightsei.emit` + `lightsei.send_command` for
   agents with sensitivity_level == 'pii'; per-call opt-out via
   redact=False; handoff_span emits the linking event.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import pytest

import lightsei
from lightsei import _capabilities, _client
from lightsei._redaction import (
    BUILTIN_DETECTORS,
    _luhn_valid,
    redact,
    redact_payload,
    register_redactor,
)


@pytest.fixture(autouse=True)
def _reset_client_between_tests():
    yield
    _client._reset_for_tests()


# ---------- Built-in detectors ---------- #


def test_email_detector_replaces_standard_shape():
    out = redact("ping alice@example.com about it", detectors=["email"])
    assert "alice@example.com" not in out
    assert "[redacted-email]" in out


def test_email_detector_leaves_non_matches_alone():
    for ok in (
        "no email here",
        "alice at example dot com",  # obfuscated; we don't try to catch
        "what@",  # malformed
    ):
        assert redact(ok, detectors=["email"]) == ok


def test_phone_detector_us_shapes():
    cases = [
        ("call 415-555-1234 please", "[redacted-phone]"),
        ("(415) 555-1234", "[redacted-phone]"),
        ("+1 415.555.1234", "[redacted-phone]"),
        ("1-415-555-1234", "[redacted-phone]"),
    ]
    for inp, want_in in cases:
        out = redact(inp, detectors=["phone"])
        assert want_in in out, (inp, out)


def test_phone_detector_skips_bare_10_digit_runs():
    """Don't over-redact dates / ids / order numbers. Requires
    separators OR parens to look phone-shaped."""
    for ok in (
        "order 4155551234 confirmed",  # no separators
        "20251217 launch date",
    ):
        assert "[redacted-phone]" not in redact(ok, detectors=["phone"])


def test_ssn_detector_hyphenated_only():
    """Hyphenated SSN catches; bare 9-digit runs deliberately don't
    (zip+4 / order numbers would false-positive)."""
    assert "[redacted-ssn]" in redact(
        "SSN 123-45-6789 on file", detectors=["ssn"],
    )
    assert "[redacted-ssn]" not in redact(
        "code 123456789", detectors=["ssn"],
    )


def test_luhn_valid_checks_known_test_numbers():
    """Standard test card numbers. Visa: 4111111111111111;
    Mastercard: 5500000000000004; Amex: 340000000000009."""
    for card in ("4111111111111111", "5500000000000004", "340000000000009"):
        assert _luhn_valid(card), card
    # A digit-by-digit equivalent that isn't Luhn-valid.
    assert not _luhn_valid("4111111111111112")


def test_credit_card_detector_redacts_luhn_valid_numbers():
    out = redact("card 4111-1111-1111-1111 on file", detectors=["credit_card"])
    assert "[redacted-card]" in out
    assert "4111" not in out


def test_credit_card_detector_skips_luhn_invalid():
    """Random 16-digit runs that don't pass Luhn stay intact —
    dramatically reduces false positives vs a pure-regex detector."""
    out = redact("order 4111111111111112 placed", detectors=["credit_card"])
    assert "[redacted-card]" not in out
    assert "4111111111111112" in out


def test_redact_runs_all_detectors_by_default():
    """No detectors= argument → run them all in turn."""
    raw = "email alice@x.com phone 415-555-1234 SSN 123-45-6789"
    out = redact(raw)
    assert "[redacted-email]" in out
    assert "[redacted-phone]" in out
    assert "[redacted-ssn]" in out
    assert "alice@x.com" not in out


def test_redact_passes_through_non_string():
    """Pass None / numbers / dicts unchanged. Lets callers do
    redact(x) without type-guarding when x might be None."""
    assert redact(None) is None  # type: ignore[arg-type]
    assert redact(42) == 42  # type: ignore[arg-type]


# ---------- register_redactor ---------- #


def test_register_redactor_runs_alongside_builtins():
    """A custom detector runs after the built-ins (or replaces by name)."""
    def employee_id(text: str) -> str:
        return text.replace("EMP-", "[redacted-empid-")

    register_redactor("employee_id", employee_id)
    out = redact("ticket from EMP-42 about EMP-99")
    assert "EMP-42" not in out
    assert "[redacted-empid-42" in out


def test_register_redactor_name_collision_overrides_builtin():
    """A custom detector with the same name as a built-in replaces
    the built-in — workspaces can tighten or relax email matching, etc."""
    def stricter_email(text: str) -> str:
        # Pretend this matches nothing — the built-in would catch
        # alice@example.com.
        return text

    register_redactor("email", stricter_email)
    out = redact("alice@example.com here", detectors=["email"])
    assert "alice@example.com" in out  # built-in was replaced


def test_register_redactor_validates_args():
    with pytest.raises(ValueError):
        register_redactor("", lambda s: s)
    with pytest.raises(ValueError):
        register_redactor("ok", "not callable")  # type: ignore[arg-type]


# ---------- redact_payload (recursive) ---------- #


def test_redact_payload_walks_dicts():
    p = {"to": "alice@example.com", "subject": "ping", "body": "call 415-555-1234"}
    out = redact_payload(p)
    assert "[redacted-email]" in out["to"]
    assert "[redacted-phone]" in out["body"]
    assert out["subject"] == "ping"


def test_redact_payload_walks_lists():
    p = ["alice@x.com", "bob@y.com", 42, None]
    out = redact_payload(p)
    assert out[0] == "[redacted-email]"
    assert out[1] == "[redacted-email]"
    assert out[2] == 42
    assert out[3] is None


def test_redact_payload_walks_nested():
    p = {
        "rows": [
            {"email": "alice@x.com", "score": 99},
            {"email": "bob@y.com", "score": 100},
        ],
        "meta": {"count": 2},
    }
    out = redact_payload(p)
    assert out["rows"][0]["email"] == "[redacted-email]"
    assert out["rows"][0]["score"] == 99
    assert out["meta"] == {"count": 2}


def test_redact_payload_does_not_mutate_input():
    """Caller's original payload is left intact so it stays usable
    for the caller's own logging / debugging."""
    p = {"to": "alice@x.com"}
    redact_payload(p)
    assert p == {"to": "alice@x.com"}


# ---------- Auto-redact lifecycle (emit + send_command) ---------- #


@contextmanager
def pii_backend(
    received: list[dict],
    *,
    sensitivity_level: str = "pii",
) -> Iterator[str]:
    """Fake backend that returns the agent as pii so emit + send_command
    auto-redact. Captures every POST so the test can inspect what
    actually went over the wire."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/agents/"):
                name = self.path[len("/agents/"):]
                body = json.dumps({
                    "name": name,
                    "system_prompt": None,
                    "sensitivity_level": sensitivity_level,
                    "capabilities": ["internet", "send_command"],
                    "dispatches_cross_zone": True,  # so cross-zone gate doesn't fire
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
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}

            if self.path.endswith("/instances/heartbeat"):
                body = json.dumps({
                    "status": "active",
                    "capabilities": ["internet", "send_command"],
                    "sensitivity_level": sensitivity_level,
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/events":
                received.append({"_path": self.path, **data})
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":1,"status":"ok"}')
                return
            if self.path.startswith("/agents/") and self.path.endswith("/commands"):
                received.append({"_path": self.path, **data})
                cmd = {
                    "id": "cmd-1", "target_agent": "x", "kind": data.get("kind"),
                    "payload": data.get("payload"), "status": "pending",
                    "created_at": "2026-05-17T00:00:00+00:00",
                }
                body = json.dumps(cmd).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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


def _drain_events(received: list[dict], wait_s: float = 2.0) -> list[dict]:
    """Wait until at least one event arrives at the fake backend.
    Lets the SDK's background flush thread catch up before assertions."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        events = [r for r in received if r.get("_path") == "/events"]
        if events:
            return events
        time.sleep(0.05)
    return [r for r in received if r.get("_path") == "/events"]


def test_emit_auto_redacts_for_pii_agent():
    """SDK drops emits without a run_id, so wrap in @lightsei.track to
    establish a run context — matches how user code does it in practice."""
    received: list[dict] = []
    with pii_backend(received) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url, flush_interval=0.1)
        assert _client._sensitivity_level == "pii"

        @lightsei.track
        def lookup():
            lightsei.emit("crm.lookup", {"email": "alice@example.com"})

        lookup()
        lightsei.flush(timeout=2.0)
        time.sleep(0.2)
        events = _drain_events(received)
        sent = next((e for e in events if e.get("kind") == "crm.lookup"), None)
        assert sent is not None
        assert sent["payload"]["email"] == "[redacted-email]"


def test_emit_does_not_redact_for_non_pii_agent():
    """Non-pii agents send through unmodified. The cost of doing so
    is the user's explicit choice (they didn't tag the agent pii)."""
    received: list[dict] = []
    with pii_backend(received, sensitivity_level="internal") as url:
        lightsei.init(api_key="k", agent_name="ops", base_url=url, flush_interval=0.1)

        @lightsei.track
        def lookup():
            lightsei.emit("ops.note", {"email": "alice@example.com"})

        lookup()
        lightsei.flush(timeout=2.0)
        time.sleep(0.2)
        events = _drain_events(received)
        sent = next((e for e in events if e.get("kind") == "ops.note"), None)
        assert sent is not None
        assert sent["payload"]["email"] == "alice@example.com"


def test_emit_opt_out_with_redact_false():
    """Per-call opt-out for the audit-trail case where the operator
    genuinely needs the raw value."""
    received: list[dict] = []
    with pii_backend(received) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url, flush_interval=0.1)

        @lightsei.track
        def audit():
            lightsei.emit(
                "crm.audit", {"email": "alice@example.com"},
                redact=False,
            )

        audit()
        lightsei.flush(timeout=2.0)
        time.sleep(0.2)
        events = _drain_events(received)
        sent = next((e for e in events if e.get("kind") == "crm.audit"), None)
        assert sent is not None
        assert sent["payload"]["email"] == "alice@example.com"


def test_send_command_auto_redacts_payload_for_pii_agent():
    received: list[dict] = []
    with pii_backend(received) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url, flush_interval=0.1)
        lightsei.send_command(
            "hermes", "hermes.post",
            {"text": "ping alice@example.com about it"},
        )
    dispatches = [r for r in received if "/commands" in r.get("_path", "")]
    assert dispatches
    assert "[redacted-email]" in dispatches[0]["payload"]["text"]
    assert "alice@example.com" not in dispatches[0]["payload"]["text"]


def test_send_command_opt_out_with_redact_false():
    received: list[dict] = []
    with pii_backend(received) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url, flush_interval=0.1)
        lightsei.send_command(
            "hermes", "hermes.post",
            {"text": "alice@example.com"},
            redact=False,
        )
    dispatches = [r for r in received if "/commands" in r.get("_path", "")]
    assert dispatches
    assert dispatches[0]["payload"]["text"] == "alice@example.com"


# ---------- handoff_span ---------- #


def test_handoff_span_emits_linking_event():
    """The operator chat surface (Phase 21) calls this to mark a
    human-mediated translation between two zones. SDK emits a
    `handoff` event with the linking metadata."""
    received: list[dict] = []
    with pii_backend(received) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url, flush_interval=0.1)

        @lightsei.track
        def do_handoff():
            lightsei.handoff_span(
                from_run="run-A",
                to_run="run-B",
                sanitized_prompt="customer wants help with order",
                notes="dropped name + email",
            )

        do_handoff()
        lightsei.flush(timeout=2.0)
        time.sleep(0.2)
        events = _drain_events(received)
        handoffs = [e for e in events if e.get("kind") == "handoff"]
        assert handoffs
        h = handoffs[0]
        assert h["payload"]["from_run"] == "run-A"
        assert h["payload"]["to_run"] == "run-B"
        assert h["payload"]["sanitized_prompt"] == "customer wants help with order"
        assert h["payload"]["notes"] == "dropped name + email"


def test_handoff_span_does_not_redact_sanitized_prompt():
    """`sanitized_prompt` is by contract already clean. If a caller
    accidentally puts a real email in there, that's their bug — the
    SDK shouldn't double-redact (could mangle deliberate
    `[redacted-email]` placeholders typed by the operator)."""
    received: list[dict] = []
    with pii_backend(received) as url:
        lightsei.init(api_key="k", agent_name="crm", base_url=url, flush_interval=0.1)

        @lightsei.track
        def do_handoff():
            lightsei.handoff_span(
                from_run="r1", to_run="r2",
                sanitized_prompt="operator typed [redacted-email] explicitly",
            )

        do_handoff()
        lightsei.flush(timeout=2.0)
        time.sleep(0.2)
        events = _drain_events(received)
        handoffs = [e for e in events if e.get("kind") == "handoff"]
        assert handoffs
        # The literal placeholder typed by the operator survives intact.
        assert (
            handoffs[0]["payload"]["sanitized_prompt"]
            == "operator typed [redacted-email] explicitly"
        )


# ---------- BUILTIN_DETECTORS surface ---------- #


def test_builtin_detectors_exposes_all_four():
    """Documenting the public surface; if a built-in is renamed or
    removed the test fails loud."""
    assert set(BUILTIN_DETECTORS.keys()) == {
        "email", "phone", "ssn", "credit_card",
    }
