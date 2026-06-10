"""Endpoint tests for the on-demand feeder digest.

Driven through the FastAPI TestClient + a real signed-up workspace, so
auth + the JSONB payload round-trip are exercised end to end.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from db import session_scope
from models import Event
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


def test_status_surfaces_latest_written_summary(client, alice):
    h = auth_headers(alice["session_token"])
    ws_id = alice["workspace"]["id"]

    # Simulate the BI assistant having produced a summary (the event it
    # emits after handling bi.summarize).
    with session_scope() as s:
        s.add(Event(
            workspace_id=ws_id,
            run_id=str(uuid.uuid4()),
            agent_name="bi",
            kind="bi.summary",
            payload={"kind": "summary", "summary": "Leads up 20% this week."},
            timestamp=datetime.now(timezone.utc),
        ))

    r = client.get("/workspaces/me/feeder/digest/status", headers=h)
    body = r.json()
    assert body["latest_summary"] is not None
    assert body["latest_summary"]["text"] == "Leads up 20% this week."
    assert body["latest_summary"]["kind"] == "summary"
    assert body["latest_summary"]["produced_at"]


def test_digest_endpoint_requires_auth(client):
    r = client.post("/workspaces/me/feeder/digest")
    assert r.status_code in (401, 403)
