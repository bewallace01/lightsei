"""Phase 30.3.c: tests for the team-conversations endpoints.

Surfaces:

1. POST /workspaces/me/team-conversations creates a conversation
   scoped to the caller's workspace + returns the serialized row.
2. GET /workspaces/me/team-conversations lists only the caller's
   conversations (workspace isolation).
3. GET /team-conversations/{id} returns the conversation + messages,
   404s on someone else's id.
4. POST /team-conversations/{id}/messages writes a user row, runs the
   router (stubbed), writes a router row + one pending assistant row
   per picked agent; auto-derives the title from the first message.
5. Router error (e.g. missing ANTHROPIC_API_KEY) lands a router row
   with status='error' + no assistant rows — channel surfaces the
   failure instead of going silent.
6. Workspace-A operator cannot POST a message into Workspace-B's
   conversation (404 not 403, by design — same pattern as threads).
7. DELETE /team-conversations/{id} hard-deletes the conversation +
   its messages (FK cascade).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import main
import secrets_crypto
import team_router
from db import session_scope
from models import Agent, TeamConversation, TeamMessage, WorkspaceSecret
from tests.conftest import auth_headers


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed_agents(workspace_id: str, names: list[str]) -> None:
    with session_scope() as s:
        for n in names:
            s.add(Agent(
                workspace_id=workspace_id,
                name=n,
                role="specialist",
                description=f"{n} role",
                capabilities=[],
                created_at=_utcnow(),
                updated_at=_utcnow(),
            ))


def _seed_anthropic_secret(workspace_id: str) -> None:
    with session_scope() as s:
        s.add(WorkspaceSecret(
            workspace_id=workspace_id,
            name="ANTHROPIC_API_KEY",
            encrypted_value=secrets_crypto.encrypt("sk-ant-fake-for-tests"),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))


def _stub_router(picks: list[tuple[str, str]], summary: str = "routed."):
    """Returns an anthropic_factory that produces a client whose
    messages.create returns a single tool_use block matching the
    given picks. Use via monkeypatch on the module-level injection
    point."""
    block = SimpleNamespace(
        type="tool_use",
        name="route_team_message",
        input={
            "summary": summary,
            "agents": [{"name": n, "reason": r} for n, r in picks],
        },
    )
    resp = SimpleNamespace(content=[block])
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: resp),
    )
    return lambda api_key: client


def _failing_router(error: Exception):
    def _raise(**kw):
        raise error

    client = SimpleNamespace(messages=SimpleNamespace(create=_raise))
    return lambda api_key: client


# ---------- Create + list + get ---------- #


def test_create_team_conversation_returns_scoped_row(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post(
        "/workspaces/me/team-conversations",
        headers=h,
        json={"title": "release-readiness"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "release-readiness"
    assert body["workspace_id"] == alice["workspace"]["id"]
    assert body["created_at"]
    assert body["updated_at"]


def test_create_defaults_title_when_omitted(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Team chat"


def test_list_only_includes_callers_conversations(client, alice, bob):
    ha = auth_headers(alice["session_token"])
    hb = auth_headers(bob["session_token"])
    client.post(
        "/workspaces/me/team-conversations", headers=ha,
        json={"title": "alice-conv"},
    )
    client.post(
        "/workspaces/me/team-conversations", headers=hb,
        json={"title": "bob-conv"},
    )
    r = client.get("/workspaces/me/team-conversations", headers=ha)
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["conversations"]]
    assert "alice-conv" in titles
    assert "bob-conv" not in titles


def test_get_conversation_returns_messages_in_order(
    client, alice, monkeypatch,
):
    h = auth_headers(alice["session_token"])
    _seed_agents(alice["workspace"]["id"], ["argus"])
    _seed_anthropic_secret(alice["workspace"]["id"])
    monkeypatch.setattr(
        main, "_TEAM_ROUTER_ANTHROPIC_FACTORY",
        _stub_router([("argus", "secret scan")]),
    )
    create = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()
    cid = create["id"]
    client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": "scan last commit"},
    )

    r = client.get(f"/team-conversations/{cid}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["conversation"]["id"] == cid
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "router", "assistant"]
    assert body["messages"][2]["agent_name"] == "argus"
    assert body["messages"][2]["status"] == "pending"


def test_get_other_workspace_conversation_is_404(client, alice, bob):
    h_alice = auth_headers(alice["session_token"])
    h_bob = auth_headers(bob["session_token"])
    create = client.post(
        "/workspaces/me/team-conversations", headers=h_alice, json={},
    ).json()
    cid = create["id"]
    r = client.get(f"/team-conversations/{cid}", headers=h_bob)
    assert r.status_code == 404


# ---------- Dispatch ---------- #


def test_post_message_writes_user_router_and_pending_assistants(
    client, alice, monkeypatch,
):
    h = auth_headers(alice["session_token"])
    workspace_id = alice["workspace"]["id"]
    _seed_agents(workspace_id, ["argus", "hermes"])
    _seed_anthropic_secret(workspace_id)
    monkeypatch.setattr(
        main, "_TEAM_ROUTER_ANTHROPIC_FACTORY",
        _stub_router(
            [("argus", "secret scan"), ("hermes", "notify ops")],
            summary="argus + hermes will handle this.",
        ),
    )
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()["id"]

    r = client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": "scan + notify"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_message"]["content"] == "scan + notify"
    assert body["router_message"]["role"] == "router"
    assert body["router_message"]["status"] == "completed"
    assert body["router_message"]["routed_agents"] == {
        "agents": [
            {"name": "argus", "reason": "secret scan"},
            {"name": "hermes", "reason": "notify ops"},
        ],
    }
    pending_names = [m["agent_name"] for m in body["pending_messages"]]
    assert pending_names == ["argus", "hermes"]
    for m in body["pending_messages"]:
        assert m["status"] == "pending"
        assert m["role"] == "assistant"

    # Persisted in db?
    with session_scope() as s:
        rows = s.execute(
            select(TeamMessage)
            .where(TeamMessage.conversation_id == cid)
            .order_by(TeamMessage.created_at)
        ).scalars().all()
    assert [r.role for r in rows] == ["user", "router", "assistant", "assistant"]


def test_post_message_auto_derives_title_from_first_message(
    client, alice, monkeypatch,
):
    h = auth_headers(alice["session_token"])
    workspace_id = alice["workspace"]["id"]
    _seed_agents(workspace_id, ["argus"])
    _seed_anthropic_secret(workspace_id)
    monkeypatch.setattr(
        main, "_TEAM_ROUTER_ANTHROPIC_FACTORY",
        _stub_router([("argus", "scan")]),
    )
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()["id"]
    client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": "scan the release branch please"},
    )
    r = client.get(f"/team-conversations/{cid}", headers=h)
    assert r.json()["conversation"]["title"] == (
        "scan the release branch please"
    )


def test_router_error_writes_error_row_and_no_assistants(
    client, alice, monkeypatch,
):
    """If Polaris can't run (missing key here), the user message
    still lands + a single router row carries the failure. The
    channel must surface this rather than going silent."""
    h = auth_headers(alice["session_token"])
    workspace_id = alice["workspace"]["id"]
    _seed_agents(workspace_id, ["argus"])
    # Deliberately NO ANTHROPIC_API_KEY: route_team_message raises
    # RouterError before any LLM call.
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()["id"]
    r = client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": "ping"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["router_message"]["status"] == "error"
    assert "ANTHROPIC_API_KEY" in body["router_message"]["error"]
    assert body["pending_messages"] == []


def test_router_llm_failure_writes_error_row_after_mid_request_commit(
    client, alice, monkeypatch,
):
    """route_team_message commits before the provider call. If that
    call fails, the endpoint must still write a router error row so
    the already-committed user message is not left alone."""
    h = auth_headers(alice["session_token"])
    workspace_id = alice["workspace"]["id"]
    _seed_agents(workspace_id, ["argus"])
    _seed_anthropic_secret(workspace_id)
    monkeypatch.setattr(
        main, "_TEAM_ROUTER_ANTHROPIC_FACTORY",
        _failing_router(RuntimeError("provider timed out")),
    )
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()["id"]

    r = client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": "please investigate prod"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_message"]["content"] == "please investigate prod"
    assert body["router_message"]["role"] == "router"
    assert body["router_message"]["status"] == "error"
    assert "provider timed out" in body["router_message"]["error"]
    assert body["pending_messages"] == []

    with session_scope() as s:
        rows = s.execute(
            select(TeamMessage)
            .where(TeamMessage.conversation_id == cid)
            .order_by(TeamMessage.created_at)
        ).scalars().all()
    assert [m.role for m in rows] == ["user", "router"]
    assert rows[1].status == "error"


# ---------- Isolation + delete ---------- #


def test_post_message_into_other_workspace_is_404(
    client, alice, bob,
):
    h_alice = auth_headers(alice["session_token"])
    h_bob = auth_headers(bob["session_token"])
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h_alice, json={},
    ).json()["id"]
    r = client.post(
        f"/team-conversations/{cid}/messages",
        headers=h_bob, json={"content": "intrusion"},
    )
    assert r.status_code == 404
    with session_scope() as s:
        rows = s.execute(
            select(TeamMessage)
            .where(TeamMessage.conversation_id == cid)
        ).scalars().all()
    assert rows == []


def test_delete_conversation_cascades_to_messages(
    client, alice, monkeypatch,
):
    h = auth_headers(alice["session_token"])
    workspace_id = alice["workspace"]["id"]
    _seed_agents(workspace_id, ["argus"])
    _seed_anthropic_secret(workspace_id)
    monkeypatch.setattr(
        main, "_TEAM_ROUTER_ANTHROPIC_FACTORY",
        _stub_router([("argus", "scan")]),
    )
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()["id"]
    client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": "hi"},
    )

    r = client.delete(f"/team-conversations/{cid}", headers=h)
    assert r.status_code == 200
    assert r.json() == {"deleted": cid}

    with session_scope() as s:
        assert s.get(TeamConversation, cid) is None
        msgs = s.execute(
            select(TeamMessage).where(TeamMessage.conversation_id == cid)
        ).scalars().all()
        assert msgs == []
