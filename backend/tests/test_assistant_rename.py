"""Phase 35.2: per-workspace assistant rename."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from db import session_scope
from models import Agent, Event
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provision(ws_id: str, *names: str) -> None:
    with session_scope() as s:
        for n in names:
            s.add(Agent(workspace_id=ws_id, name=n, role="executor",
                        created_at=_now(), updated_at=_now()))


def test_rename_updates_team_status(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "reputation")
    h = auth_headers(alice["session_token"])

    # Default star name first.
    body = client.get("/workspaces/me/team/status", headers=h).json()
    rep = [a for a in body["assistants"] if a["name"] == "reputation"][0]
    assert rep["display_name"] == "Lyra"
    assert rep["is_custom_name"] is False

    # Rename it.
    r = client.patch("/workspaces/me/assistants/reputation", headers=h,
                     json={"display_name": "Reviews"})
    assert r.status_code == 200, r.text
    assert r.json()["assistant"]["display_name"] == "Reviews"
    assert r.json()["assistant"]["role"] == "Reputation"
    assert r.json()["assistant"]["is_custom_name"] is True

    body = client.get("/workspaces/me/team/status", headers=h).json()
    rep = [a for a in body["assistants"] if a["name"] == "reputation"][0]
    assert rep["display_name"] == "Reviews"
    assert rep["role"] == "Reputation"  # role unchanged
    assert rep["is_custom_name"] is True


def test_blank_name_resets_to_default(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi")
    h = auth_headers(alice["session_token"])

    client.patch("/workspaces/me/assistants/bi", headers=h,
                 json={"display_name": "Numbers"})
    client.patch("/workspaces/me/assistants/bi", headers=h,
                 json={"display_name": "  "})  # reset

    body = client.get("/workspaces/me/team/status", headers=h).json()
    bi = [a for a in body["assistants"] if a["name"] == "bi"][0]
    assert bi["display_name"] == "Altair"  # back to the star default
    assert bi["is_custom_name"] is False


def test_rename_unknown_assistant_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch("/workspaces/me/assistants/inbox", headers=h,
                     json={"display_name": "Mail"})
    assert r.status_code == 404  # not provisioned in this workspace


def test_agents_endpoint_carries_constellation_identity(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "reputation")
    h = auth_headers(alice["session_token"])

    rep = [a for a in client.get("/agents", headers=h).json()["agents"]
           if a["name"] == "reputation"][0]
    assert rep["display_name"] == "Lyra"
    assert rep["assistant_role"] == "Reputation"
    assert rep["is_custom_name"] is False

    client.patch("/workspaces/me/assistants/reputation", headers=h,
                 json={"display_name": "Reviews"})
    rep = [a for a in client.get("/agents", headers=h).json()["agents"]
           if a["name"] == "reputation"][0]
    assert rep["display_name"] == "Reviews"
    assert rep["is_custom_name"] is True


def test_ask_attributes_answer_to_bi_assistant(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi")
    h = auth_headers(alice["session_token"])

    body = client.post("/workspaces/me/ask", headers=h,
                       json={"question": "how are we?"}).json()
    assert body["assistant"]["name"] == "Altair"
    assert body["assistant"]["role"] == "Business Intelligence"

    # Rename BI -> the attribution follows.
    client.patch("/workspaces/me/assistants/bi", headers=h,
                 json={"display_name": "Numbers"})
    body = client.post("/workspaces/me/ask", headers=h,
                       json={"question": "again?"}).json()
    assert body["assistant"]["name"] == "Numbers"


def test_feed_reflects_renamed_assistant(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "reputation")
    h = auth_headers(alice["session_token"])
    client.patch("/workspaces/me/assistants/reputation", headers=h,
                 json={"display_name": "Reviews"})

    with session_scope() as s:
        s.add(Event(workspace_id=ws_id, run_id=str(uuid.uuid4()),
                    agent_name="reputation", kind="reputation.analyzed",
                    payload={"sentiment": "negative", "author": "Dana",
                             "severity": "error"},
                    timestamp=_now()))

    items = client.get("/workspaces/me/feed", headers=h).json()["items"]
    assert items[0]["assistant_name"] == "Reviews"
    assert items[0]["assistant_label"] == "Reviews · Reputation"
