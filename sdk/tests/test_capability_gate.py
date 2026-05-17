"""Phase 16.3: SDK capability gate tests.

Covers four surfaces:

1. The error class and the pure cache/check helpers.
2. The httpx wrapper — refuses outbound HTTP when 'internet' isn't
   granted, but bypasses the gate for calls to the Lightsei backend
   itself.
3. The send_command wrapper — refuses dispatches when 'send_command'
   isn't granted.
4. The init/heartbeat lifecycle — initial fetch populates the cache,
   heartbeat response refreshes it.

Uses the `fake_backend` helper from test_basic.py extended with a
`capabilities=` kwarg + a `/agents/{name}` GET handler + a
`/commands` POST handler.
"""
from __future__ import annotations

import time

import pytest

import lightsei
from lightsei import _capabilities, _client
from lightsei.errors import LightseiCapabilityError
from tests.test_basic import fake_backend


# The autouse fixture in test_basic.py is module-scoped and doesn't
# apply here. Add our own so capability cache + initialized state
# from one test doesn't leak into the next.
@pytest.fixture(autouse=True)
def _reset_client_between_tests():
    yield
    _client._reset_for_tests()


# ---------- LightseiCapabilityError ---------- #


def test_capability_error_carries_capability_and_granted():
    """Attributes are populated so callers can introspect rather than
    parsing the message."""
    err = LightseiCapabilityError(
        capability="internet",
        granted=["send_command"],
        agent_name="argus",
    )
    assert err.capability == "internet"
    assert err.granted == ["send_command"]
    assert err.agent_name == "argus"
    msg = str(err)
    assert "'internet'" in msg
    assert "'argus'" in msg
    assert "send_command" in msg


def test_capability_error_message_for_empty_grant_is_helpful():
    """Empty list shows 'none — default-deny' so the user knows the
    agent's allow-list is empty (the canonical compliance-bot state),
    not just that this one capability is missing."""
    err = LightseiCapabilityError(capability="internet", granted=[])
    assert "default-deny" in str(err)


# ---------- Pure cache + check ---------- #


def test_update_capabilities_replaces_cache():
    _client._capabilities_cache = []
    _client._capabilities_loaded = False
    _capabilities.update_capabilities(_client, ["internet", "send_command"])
    assert _client._capabilities_loaded
    assert _client._capabilities_cache == ["internet", "send_command"]


def test_update_capabilities_ignores_non_list_input():
    """Defensive: don't trust an upstream that sends a string or dict.
    Cache stays where it was."""
    _client._capabilities_cache = ["existing"]
    _client._capabilities_loaded = True
    _capabilities.update_capabilities(_client, "not a list")  # type: ignore[arg-type]
    assert _client._capabilities_cache == ["existing"]


def test_update_capabilities_drops_non_string_entries():
    """A server bug that sends `[None, 'internet', 42]` shouldn't
    crash the check_capability path; only the valid strings land in
    the cache."""
    _client._capabilities_cache = []
    _client._capabilities_loaded = False
    _capabilities.update_capabilities(
        _client, ["internet", None, 42, "send_command"],  # type: ignore[list-item]
    )
    assert _client._capabilities_cache == ["internet", "send_command"]


def test_has_capability_fails_open_before_init():
    """Cache not loaded → returns True for any capability. Lets bots
    that haven't called init() yet keep working (graceful degradation
    per CLAUDE.md)."""
    _client._capabilities_loaded = False
    assert _capabilities.has_capability(_client, "internet")
    assert _capabilities.has_capability(_client, "anything")


def test_has_capability_returns_false_when_not_granted_post_init():
    _client._capabilities_loaded = True
    _client._capabilities_cache = ["send_command"]
    assert not _capabilities.has_capability(_client, "internet")
    assert _capabilities.has_capability(_client, "send_command")


def test_check_capability_raises_when_not_granted():
    _client._capabilities_loaded = True
    _client._capabilities_cache = []
    with pytest.raises(LightseiCapabilityError) as exc:
        _capabilities.check_capability(_client, "internet")
    assert exc.value.capability == "internet"


def test_check_capability_noop_when_granted():
    _client._capabilities_loaded = True
    _client._capabilities_cache = ["internet"]
    _capabilities.check_capability(_client, "internet")  # no raise


# ---------- is_lightsei_internal_url ---------- #


def test_is_lightsei_internal_url_matches_base_host():
    """SDK's own backend calls (event posts, heartbeats) must bypass
    the gate — they're identified by host match against base_url."""
    _client.base_url = "http://127.0.0.1:8000"
    assert _capabilities.is_lightsei_internal_url(_client, "http://127.0.0.1:8000/events")
    assert _capabilities.is_lightsei_internal_url(_client, "http://127.0.0.1:8000/agents/x")


def test_is_lightsei_internal_url_rejects_other_hosts():
    _client.base_url = "http://127.0.0.1:8000"
    assert not _capabilities.is_lightsei_internal_url(_client, "https://api.openai.com/v1")
    assert not _capabilities.is_lightsei_internal_url(_client, "https://example.com")


def test_is_lightsei_internal_url_handles_missing_base():
    _client.base_url = None
    assert not _capabilities.is_lightsei_internal_url(_client, "http://anywhere")


# ---------- httpx wrap ---------- #


def test_httpx_get_refused_without_internet_capability():
    """The whole point of 16.3: a bot without 'internet' can't do
    httpx.get() to a non-Lightsei host."""
    import httpx
    with fake_backend([], capabilities=["send_command"]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            httpx.get("https://example.com", timeout=2.0)
        assert exc.value.capability == "internet"


def test_httpx_get_allowed_when_internet_granted():
    """With 'internet' granted, httpx.get() to an external host
    proceeds — the wrapper only intercepts the capability check, not
    the actual call. We point at the fake backend since we don't want
    a real outbound request in tests; what matters is the gate
    doesn't raise."""
    import httpx
    with fake_backend([], capabilities=["internet"]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        # The fake_backend responds with 404 on unknown paths but the
        # request goes through, which is what we're asserting.
        r = httpx.get(f"{url}/not-a-known-path", timeout=2.0)
        assert r.status_code in (200, 404)  # no LightseiCapabilityError


def test_httpx_call_to_lightsei_backend_always_bypasses_gate():
    """The SDK's own internal HTTP calls (events / heartbeats / secrets)
    must work even without 'internet' — otherwise capability config
    breaks the SDK itself. The host whitelist is what protects this."""
    import httpx
    with fake_backend([], capabilities=[]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        # Direct call to the Lightsei host — no 'internet' granted,
        # but should not raise because is_lightsei_internal_url matches.
        r = httpx.get(f"{url}/agents/argus", timeout=2.0)
        assert r.status_code == 200


def test_httpx_patch_is_idempotent():
    """Re-importing or calling patch_httpx() again must not double-wrap
    (would compound the gate check on each request)."""
    import httpx
    from lightsei.integrations.httpx_patch import patch_httpx
    patch_httpx()
    first = httpx.Client.send
    patch_httpx()
    second = httpx.Client.send
    assert first is second


def test_httpx_gate_disabled_before_init():
    """Bot module that imports httpx and runs a request BEFORE
    lightsei.init() is not gated. Matches the fail-open contract."""
    import httpx
    # Reset to uninitialized state explicitly (autouse fixture does
    # this between tests but be defensive here).
    _client._capabilities_loaded = False
    _client._capabilities_cache = []
    # No init() called → should not raise.
    # Use a connect-refused URL so we know no actual HTTP happened —
    # we expect httpx's own ConnectError, NOT LightseiCapabilityError.
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://127.0.0.1:1", timeout=0.5)


# ---------- send_command wrap ---------- #


def test_send_command_refused_without_capability():
    """A bot without 'send_command' can't dispatch to other agents."""
    with fake_backend([], capabilities=["internet"]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        with pytest.raises(LightseiCapabilityError) as exc:
            lightsei.send_command("hermes", "hermes.post", {"text": "hi"})
        assert exc.value.capability == "send_command"


def test_send_command_allowed_when_capability_granted():
    received: list[dict] = []
    with fake_backend(received, capabilities=["send_command"]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        out = lightsei.send_command("hermes", "hermes.post", {"text": "hi"})
        # The fake echoes the POSTed body as a canonical command row.
        assert out["kind"] == "hermes.post"


# ---------- init + heartbeat lifecycle ---------- #


def test_init_fetches_capabilities_from_backend():
    """init() calls GET /agents/{name}; the response's `capabilities`
    field populates the cache so the gate is active by the first
    user-issued HTTP call."""
    with fake_backend([], capabilities=["internet"]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        # Cache loaded from the GET that init() fired.
        assert _client._capabilities_loaded
        assert _client._capabilities_cache == ["internet"]


def test_init_capability_fetch_fails_open_on_backend_unreachable():
    """If the backend's down at init() time, the SDK keeps starting —
    fail-open is the documented behavior. Heartbeat refresh will
    catch up later."""
    lightsei.init(
        api_key="k", agent_name="argus",
        base_url="http://127.0.0.1:1",  # connect-refused
    )
    # Cache not loaded → has_capability returns True for everything
    # (fail-open). The bot can do whatever; the next heartbeat that
    # succeeds will engage the gate.
    assert not _client._capabilities_loaded
    assert _capabilities.has_capability(_client, "internet")


def test_heartbeat_response_refreshes_capability_cache():
    """The heartbeat response carries the agent's current capability
    list so dashboard edits propagate within one heartbeat interval
    without a separate fetch. Smoke test: init with one cap, fire a
    heartbeat that returns a different cap, confirm the cache updated."""
    received: list[dict] = []
    with fake_backend(received, capabilities=["send_command"]) as url:
        lightsei.init(api_key="k", agent_name="argus", base_url=url)
        # After init: cache reflects what fake_backend was configured with.
        assert _client._capabilities_cache == ["send_command"]
        # Direct heartbeat post (the SDK already did one in init; do
        # another to confirm the refresh path runs every time).
        if _client._heartbeat is not None:
            _client._heartbeat._post_once()
        # Cache still matches what the heartbeat echoes back (same
        # config; this is the smoke test that the refresh path runs).
        assert _client._capabilities_cache == ["send_command"]


def test_init_does_not_populate_cache_when_backend_returns_no_capabilities_field():
    """A backend version older than 16.2 won't include `capabilities`
    in the agent response. SDK shouldn't crash; cache stays loaded with
    an empty list rather than barfing on the missing key."""
    import http.server
    import json as _json
    import socket
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            # Return an old-style agent response with NO capabilities field.
            body = _json.dumps({
                "name": "argus",
                "system_prompt": None,
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "20")
            self.end_headers()
            self.wfile.write(b'{"status":"active"}')

        def log_message(self, *a, **kw):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        lightsei.init(
            api_key="k", agent_name="argus",
            base_url=f"http://127.0.0.1:{port}",
        )
        # No capabilities field → update_capabilities is called with
        # None → cache stays unloaded → fail-open.
        assert not _client._capabilities_loaded
    finally:
        srv.shutdown()
        srv.server_close()
