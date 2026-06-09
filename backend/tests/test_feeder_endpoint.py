"""Endpoint tests for the on-demand feeder digest.

Driven through the FastAPI TestClient + a real signed-up workspace, so
auth + the JSONB payload round-trip are exercised end to end.
"""
from __future__ import annotations

from tests.conftest import auth_headers


def test_run_digest_queues_command(client, alice):
    h = auth_headers(alice["session_token"])

    r = client.post("/workspaces/me/feeder/digest", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["command_id"]
    # Fresh workspace has not deployed the BI assistant yet.
    assert body["bi_assistant_deployed"] is False
    assert body["note"]  # explains the pending-into-the-void case


def test_status_reflects_last_digest(client, alice):
    h = auth_headers(alice["session_token"])

    # No digest yet.
    r = client.get("/workspaces/me/feeder/digest/status", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["last_digest"] is None

    # Generate one, then status should surface it.
    client.post("/workspaces/me/feeder/digest", headers=h)
    r = client.get("/workspaces/me/feeder/digest/status", headers=h)
    body = r.json()
    assert body["last_digest"] is not None
    assert body["last_digest"]["status"] == "pending"
    assert body["period_days"] == 7


def test_digest_endpoint_requires_auth(client):
    r = client.post("/workspaces/me/feeder/digest")
    assert r.status_code in (401, 403)
