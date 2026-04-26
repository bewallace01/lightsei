"""Body-size cap and rate limits."""
import uuid

import pytest

import limits
from tests.conftest import auth_headers


def test_body_size_cap_returns_413(client, alice):
    """Sending Content-Length above MAX_BODY_BYTES should be rejected before
    the body is parsed."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Build a payload that's just over the cap. The default is 1 MB so we
    # don't actually have to ship that many bytes — the middleware reads
    # Content-Length, not the body. Send a fake header.
    big = b"x" * (limits.MAX_BODY_BYTES + 1)
    r = client.post(
        "/events",
        content=big,
        headers={**h, "content-type": "application/json"},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_signup_rate_limit_per_ip(client, monkeypatch):
    """Signup attempts from a single IP get throttled to prevent enumeration
    and hash-grinding."""
    monkeypatch.setattr(limits, "SIGNUP_LIMIT_PER_MIN", 3)

    base = {
        "password": "hunter22hunter22",
        "workspace_name": "ws",
    }
    # First 3 attempts pass (or 409 — duplicate email is fine, both count
    # against the rate limit).
    for i in range(3):
        client.post(
            "/auth/signup",
            json={**base, "email": f"u{i}@x.com"},
        )

    # 4th attempt is throttled.
    r = client.post(
        "/auth/signup",
        json={**base, "email": "u4@x.com"},
    )
    assert r.status_code == 429
    assert r.headers.get("retry-after")


def test_login_rate_limit_per_ip(client, alice, monkeypatch):
    """Login brute-force protection."""
    monkeypatch.setattr(limits, "LOGIN_LIMIT_PER_MIN", 3)

    for _ in range(3):
        r = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "WRONG"},
        )
        assert r.status_code == 401  # bad password but not throttled yet

    r = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "WRONG"},
    )
    assert r.status_code == 429
    assert r.headers.get("retry-after")


def test_events_rate_limit_per_credential(client, alice, monkeypatch):
    """The /events ingest path is the highest-volume vector. A runaway bot
    on a single api_key throttles itself without taking out the dashboard."""
    monkeypatch.setattr(limits, "EVENTS_LIMIT_PER_MIN", 3)

    h = auth_headers(alice["api_key"]["plaintext"])
    body = {
        "run_id": str(uuid.uuid4()),
        "agent_name": "demo",
        "kind": "run_started",
        "payload": {},
    }
    for _ in range(3):
        r = client.post("/events", json=body, headers=h)
        assert r.status_code == 200

    r = client.post("/events", json=body, headers=h)
    assert r.status_code == 429


def test_events_limit_isolated_per_credential(client, alice, monkeypatch):
    """Two api_keys on the same workspace count separately. Critical: the
    dashboard's session must not share a counter with a bot's api_key."""
    monkeypatch.setattr(limits, "EVENTS_LIMIT_PER_MIN", 3)

    # Mint a second api key for the same workspace.
    r = client.post(
        "/workspaces/me/api-keys",
        json={"name": "second"},
        headers=auth_headers(alice["session_token"]),
    )
    second_key = r.json()["plaintext"]

    h_main = auth_headers(alice["api_key"]["plaintext"])
    h_second = auth_headers(second_key)
    body = {
        "run_id": str(uuid.uuid4()),
        "agent_name": "demo",
        "kind": "run_started",
        "payload": {},
    }

    # Burn the main key's quota.
    for _ in range(3):
        assert client.post("/events", json=body, headers=h_main).status_code == 200
    assert client.post("/events", json=body, headers=h_main).status_code == 429

    # The second key still has its full quota.
    body2 = {**body, "run_id": str(uuid.uuid4())}
    for _ in range(3):
        assert client.post("/events", json=body2, headers=h_second).status_code == 200


def test_rate_limit_unit_returns_retry_after():
    """Direct unit test of the counter, independent of FastAPI."""
    from fastapi import HTTPException

    # 2 per 60s
    limits.rate_limit("k", limit=2, window_s=60.0)
    limits.rate_limit("k", limit=2, window_s=60.0)
    with pytest.raises(HTTPException) as exc:
        limits.rate_limit("k", limit=2, window_s=60.0)
    assert exc.value.status_code == 429
    retry = exc.value.headers["Retry-After"]
    assert int(retry) >= 1
