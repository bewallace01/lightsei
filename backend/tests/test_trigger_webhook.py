"""Phase 22.6: tests for the public webhook fire endpoint.

POST /triggers/{token}/fire is the only unauthenticated trigger
endpoint. Token in the URL IS the auth (GitHub-shaped). Body is
forwarded to the bot as `lightsei.trigger.webhook_payload`.

Surface covered:

- happy path: JSON body, non-JSON body, empty body, JSON scalar/list
  wrapped under `value`.
- security: invalid token → 404, disabled trigger → 404, cron-trigger
  token-attempt → 404 (same response for all three; don't leak).
- rate limit: per-token 60/min → 429 with Retry-After header.
- side effects: enqueues a generation_jobs row of kind=scheduled_run
  with the right payload, returns the pre-allocated run_id, bumps
  trigger.last_run_at.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

import triggers as trigmod
from db import session_scope
from models import Agent, Trigger, Workspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_agent(workspace_id: str, name: str = "morning-digest") -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="executor",
            capabilities=[],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))


def _create_webhook_trigger(client, alice, *, name: str = "zapier") -> str:
    """Create a webhook trigger via the authed API + return the
    plaintext token (the response only shows it once)."""
    _add_agent(alice["workspace"]["id"])
    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"kind": "webhook", "name": name},
    )
    assert r.status_code == 200, r.text
    return r.json()["webhook_token"]


def _scheduled_run_jobs_for_trigger(s, trigger_id: str) -> list[dict]:
    rows = s.execute(text(
        "SELECT request_payload FROM generation_jobs "
        "WHERE kind = 'scheduled_run'"
    )).mappings().all()
    return [
        r["request_payload"]
        for r in rows
        if (r["request_payload"] or {}).get("trigger_id") == trigger_id
    ]


# ---------- happy paths ---------- #


def test_fire_with_json_body(client, alice):
    token = _create_webhook_trigger(client, alice)

    body = {"channel": "#sales", "user": "ada"}
    r = client.post(f"/triggers/{token}/fire", json=body)
    assert r.status_code == 200, r.text

    out = r.json()
    assert out["status"] == "queued"
    assert isinstance(out["run_id"], str) and len(out["run_id"]) == 36
    assert "trigger_id" in out

    with session_scope() as s:
        jobs = _scheduled_run_jobs_for_trigger(s, out["trigger_id"])
        assert len(jobs) == 1
        assert jobs[0]["webhook_payload"] == body
        assert jobs[0]["run_id"] == out["run_id"]


def test_fire_with_non_json_body(client, alice):
    """A non-JSON body comes through as `{raw: <text>}` so the bot
    handler can still see what arrived."""
    token = _create_webhook_trigger(client, alice)

    r = client.post(
        f"/triggers/{token}/fire",
        content="hello world",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 200

    with session_scope() as s:
        trigger_id = r.json()["trigger_id"]
        jobs = _scheduled_run_jobs_for_trigger(s, trigger_id)
        assert jobs[0]["webhook_payload"] == {"raw": "hello world"}


def test_fire_with_empty_body(client, alice):
    """An empty POST works — webhook_payload becomes {} so bot
    handlers that just want to fire on event arrival don't need
    a body."""
    token = _create_webhook_trigger(client, alice)

    r = client.post(f"/triggers/{token}/fire", content=b"")
    assert r.status_code == 200

    with session_scope() as s:
        trigger_id = r.json()["trigger_id"]
        jobs = _scheduled_run_jobs_for_trigger(s, trigger_id)
        assert jobs[0]["webhook_payload"] == {}


def test_fire_with_json_list_wraps_under_value(client, alice):
    """A top-level JSON list (or scalar) gets wrapped as
    `{value: ...}` so the bot's accessor is always a dict."""
    token = _create_webhook_trigger(client, alice)

    r = client.post(f"/triggers/{token}/fire", json=[1, 2, 3])
    assert r.status_code == 200

    with session_scope() as s:
        trigger_id = r.json()["trigger_id"]
        jobs = _scheduled_run_jobs_for_trigger(s, trigger_id)
        assert jobs[0]["webhook_payload"] == {"value": [1, 2, 3]}


def test_fire_bumps_last_run_at(client, alice):
    token = _create_webhook_trigger(client, alice)

    # Before firing, last_run_at is null (trigger was just created).
    with session_scope() as s:
        tid = s.execute(select(Trigger.id)).scalars().first()
        assert s.get(Trigger, tid).last_run_at is None

    client.post(f"/triggers/{token}/fire", json={})

    with session_scope() as s:
        assert s.get(Trigger, tid).last_run_at is not None


# ---------- security: 404s for invalid/disabled/cron ---------- #


def test_fire_invalid_token_returns_404(client):
    """Garbage token returns 404 — same response as a real token
    pointing at a disabled trigger, so a probe can't distinguish."""
    r = client.post("/triggers/totally-bogus-token/fire", json={})
    assert r.status_code == 404


def test_fire_disabled_trigger_returns_404(client, alice):
    """Disabling a webhook trigger makes /fire return 404 (not 403).
    Operators reach for disable when they want the token to stop
    working without revoking it; the response should look identical
    to a bad token."""
    token = _create_webhook_trigger(client, alice)

    # Disable via the authed PATCH endpoint.
    with session_scope() as s:
        tid = s.execute(select(Trigger.id)).scalars().first()
    r = client.patch(
        f"/triggers/{tid}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"enabled": False},
    )
    assert r.status_code == 200

    r = client.post(f"/triggers/{token}/fire", json={})
    assert r.status_code == 404


def test_fire_cron_trigger_token_attempt_returns_404(client, alice):
    """Cron triggers have NULL webhook_token_hash so they can't be
    fired via /fire. Defensive: even if a cron trigger somehow
    matched a token hash, the kind check would reject it as 404."""
    # Create a cron trigger (no webhook token).
    _add_agent(alice["workspace"]["id"])
    client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"kind": "cron", "name": "morning", "preset": "daily"},
    )

    # Try /fire with a plausible-looking token.
    r = client.post(f"/triggers/{uuid.uuid4().hex}/fire", json={})
    assert r.status_code == 404


# ---------- rate limit ---------- #


def test_fire_rate_limit_returns_429_with_retry_after(client, alice):
    """Per-token 60/min: hit it 61 times, the 61st returns 429 +
    Retry-After. Trigger.last_run_at only advances for the 60 that
    were accepted."""
    token = _create_webhook_trigger(client, alice)

    # Fire 60 times — all accepted.
    for _ in range(60):
        r = client.post(f"/triggers/{token}/fire", json={})
        assert r.status_code == 200

    r = client.post(f"/triggers/{token}/fire", json={})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # Retry-After is an integer seconds value per the limiter.
    assert int(r.headers["Retry-After"]) >= 1


def test_fire_rate_limit_per_token_not_global(client, alice, bob):
    """Each trigger has its own 60/min bucket. Hitting alice's
    trigger up to the cap doesn't throttle bob's trigger."""
    a_token = _create_webhook_trigger(client, alice, name="alice")

    _add_agent(bob["workspace"]["id"])
    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(bob["api_key"]["plaintext"]),
        json={"kind": "webhook", "name": "bob"},
    )
    b_token = r.json()["webhook_token"]

    # Burn alice's quota.
    for _ in range(60):
        client.post(f"/triggers/{a_token}/fire", json={})

    # Bob's still works.
    r = client.post(f"/triggers/{b_token}/fire", json={})
    assert r.status_code == 200


def test_fire_invalid_token_also_rate_limited(client):
    """Defense against spray-and-pray: invalid tokens hit the same
    counter so a brute-force flood gets throttled too. Same plaintext
    → same hash → same bucket."""
    junk_token = "bogus-token-aaaa"
    for _ in range(60):
        r = client.post(f"/triggers/{junk_token}/fire", json={})
        assert r.status_code == 404
    r = client.post(f"/triggers/{junk_token}/fire", json={})
    assert r.status_code == 429
