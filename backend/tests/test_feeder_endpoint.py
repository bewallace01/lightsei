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


# ---------- feeder settings endpoints ---------- #


def test_list_feeders_defaults_enabled(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/feeders", headers=h)
    assert r.status_code == 200, r.text
    feeders = r.json()["feeders"]
    by_kind = {f["kind"]: f for f in feeders}
    assert "weekly_digest" in by_kind
    assert "cost_spike" in by_kind
    assert "inbox_gmail" in by_kind
    # Internal-data feeders default on; the inbox feeder (polls a real
    # external inbox) defaults off.
    assert by_kind["weekly_digest"]["enabled"] is True
    assert by_kind["cost_spike"]["enabled"] is True
    assert by_kind["inbox_gmail"]["enabled"] is False
    assert all(f["name"] and f["description"] for f in feeders)


def test_toggle_feeder_off_and_on(client, alice):
    h = auth_headers(alice["session_token"])

    r = client.patch(
        "/workspaces/me/feeders/weekly_digest",
        headers=h,
        json={"enabled": False},
    )
    assert r.status_code == 200, r.text
    by_kind = {f["kind"]: f for f in r.json()["feeders"]}
    assert by_kind["weekly_digest"]["enabled"] is False
    assert by_kind["cost_spike"]["enabled"] is True  # untouched

    r = client.patch(
        "/workspaces/me/feeders/weekly_digest",
        headers=h,
        json={"enabled": True},
    )
    by_kind = {f["kind"]: f for f in r.json()["feeders"]}
    assert by_kind["weekly_digest"]["enabled"] is True


def test_toggle_unknown_feeder_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/workspaces/me/feeders/not_a_feeder",
        headers=h,
        json={"enabled": False},
    )
    assert r.status_code == 404


# ---------- feeder targeting (config) endpoints ---------- #


def test_set_config_on_targetable_feeder(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/workspaces/me/feeders/reputation_reviews/config",
        headers=h,
        json={"config": {"account_id": "a1", "location_id": "l1",
                         "location_title": "Acme Downtown"}},
    )
    assert r.status_code == 200, r.text
    by_kind = {f["kind"]: f for f in r.json()["feeders"]}
    rep = by_kind["reputation_reviews"]
    assert rep["config"]["location_id"] == "l1"
    assert rep["targetable"] is True
    # Setting a target must not turn the (default-off) feeder on.
    assert rep["enabled"] is False


def test_set_url_config_on_website_feeder_normalizes(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/workspaces/me/feeders/website_health/config",
        headers=h,
        json={"config": {"url": "acme.com/contact"}},
    )
    assert r.status_code == 200, r.text
    by_kind = {f["kind"]: f for f in r.json()["feeders"]}
    site = by_kind["website_health"]
    # Owner-typed bare host comes back normalized to a fetchable URL.
    assert site["config"]["url"] == "https://acme.com/contact"
    assert site["url_target"] is True
    # Website feeder defaults on, so setting the URL leaves it on.
    assert site["enabled"] is True


def test_set_url_config_rejects_garbage_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/workspaces/me/feeders/website_health/config",
        headers=h,
        json={"config": {"url": "not a website"}},
    )
    assert r.status_code == 400


def test_set_url_config_rejects_private_targets_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/workspaces/me/feeders/website_health/config",
        headers=h,
        json={"config": {"url": "https://169.254.169.254/latest/meta-data"}},
    )
    assert r.status_code == 400


def test_set_config_on_non_targetable_feeder_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/workspaces/me/feeders/weekly_digest/config",
        headers=h,
        json={"config": {"anything": "x"}},
    )
    assert r.status_code == 400


def test_list_targets_returns_connector_locations(client, alice, monkeypatch):
    import main

    def _invoke(session, *, workspace_id, connector_type, tool_name,
                payload, source_agent):
        assert connector_type == "google_business"
        if tool_name == "list_accounts":
            return {"accounts": [{"id": "a1", "account_name": "Acme"}]}
        if tool_name == "list_locations":
            return {"locations": [
                {"id": "l1", "title": "Downtown"},
                {"id": "l2", "title": "Uptown"},
            ]}
        raise AssertionError(tool_name)

    monkeypatch.setattr(main, "invoke_connector_tool", _invoke)
    h = auth_headers(alice["session_token"])
    r = client.get(
        "/workspaces/me/feeders/reputation_reviews/targets", headers=h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    labels = {t["label"] for t in body["targets"]}
    assert labels == {"Downtown", "Uptown"}


def test_list_targets_empty_when_connector_unavailable(client, alice, monkeypatch):
    import main
    from fastapi import HTTPException

    def _invoke(*a, **k):
        raise HTTPException(status_code=400,
                            detail={"error": "connector_not_installed"})

    monkeypatch.setattr(main, "invoke_connector_tool", _invoke)
    h = auth_headers(alice["session_token"])
    r = client.get(
        "/workspaces/me/feeders/reputation_reviews/targets", headers=h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["targets"] == []
    assert body["reason"] == "connector_not_installed"


def test_list_targets_400_for_non_targetable(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/feeders/weekly_digest/targets", headers=h)
    assert r.status_code == 400
