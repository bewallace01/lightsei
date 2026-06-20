"""Design format endpoints: enqueue a design.format command (Capella) and
poll its result by command id."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from db import session_scope
from models import Event
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_design_format_enqueues(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h,
                    json={"content": "<h1>Hi</h1>", "content_type": "page",
                          "accent_color": "#0a7"})
    assert r.status_code == 200, r.text
    cid = r.json()["command_id"]
    with session_scope() as s:
        row = s.execute(text("SELECT agent_name, kind, payload FROM commands "
                             "WHERE id = :id"), {"id": cid}).mappings().first()
        assert row["agent_name"] == "design"
        assert row["kind"] == "design.format"
        assert row["payload"]["content_type"] == "page"
        assert row["payload"]["accent_color"] == "#0a7"


def test_design_format_empty_content_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h, json={"content": "  "})
    assert r.status_code == 400


def test_design_format_unknown_type_defaults_generic(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x", "content_type": "weird"}).json()["command_id"]
    with session_scope() as s:
        row = s.execute(text("SELECT payload FROM commands WHERE id=:id"),
                        {"id": cid}).mappings().first()
        assert row["payload"]["content_type"] == "generic"


def test_design_result_pending_then_formatted(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x", "content_type": "page"}).json()["command_id"]
    # Pending before Capella emits.
    r = client.get(f"/workspaces/me/design/format/{cid}", headers=h)
    assert r.json()["status"] == "pending"
    # Simulate Capella's result event.
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="design",
                    kind="design.formatted", timestamp=_now(),
                    payload={"command_id": cid, "content_type": "page",
                             "output": "<!doctype html><body>styled</body>"}))
    r2 = client.get(f"/workspaces/me/design/format/{cid}", headers=h)
    body = r2.json()
    assert body["status"] == "formatted"
    assert "styled" in body["output"]
    assert body["content_type"] == "page"


def test_design_result_failed(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x"}).json()["command_id"]
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="design",
                    kind="design.crash", timestamp=_now(),
                    payload={"command_id": cid, "error": "boom"}))
    r = client.get(f"/workspaces/me/design/format/{cid}", headers=h)
    assert r.json()["status"] == "failed"
    assert r.json()["error"] == "boom"


def test_design_result_isolated_per_workspace(client, alice, bob):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x"}).json()["command_id"]
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="design",
                    kind="design.formatted", timestamp=_now(),
                    payload={"command_id": cid, "output": "x", "content_type": "page"}))
    # bob can't see alice's result.
    r = client.get(f"/workspaces/me/design/format/{cid}",
                   headers=auth_headers(bob["session_token"]))
    assert r.json()["status"] == "pending"
