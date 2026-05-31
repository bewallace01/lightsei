"""Phase 30.3.e: tests for the team-message claim / chunk / complete
endpoints (the agent-facing side of the team channel).

These endpoints close the round-trip: 30.3.c dispatched user messages
into pending assistant rows; here each deployed agent's claim loop
picks up its own row, streams content via chunk, and finalizes via
complete (or surfaces an error).

Surfaces:

1. Claim picks the oldest pending row matching agent_name + workspace.
2. Claim skips rows for OTHER agents.
3. Claim history contains the user message + the router summary (as
   a system note), not peer agents' pending rows.
4. Claim flips status pending → in_progress (so the next claim won't
   re-grab the same row).
5. Claim respects agent.system_prompt (prepends it).
6. Claim across workspaces returns no turn (workspace isolation).
7. Empty claim returns {"turn": null}.
8. Chunk appends to msg.content.
9. Chunk 400s on already-completed msg.
10. Complete with content sets status=completed + content.
11. Complete with error sets status=error.
12. Complete in another workspace 404s.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
import secrets_crypto
from datetime import datetime, timezone
from db import session_scope
from models import Agent, TeamMessage, WorkspaceSecret
from tests.conftest import auth_headers


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed_agents(
    workspace_id: str, names: list[str], *, system_prompt: str | None = None,
) -> None:
    with session_scope() as s:
        for n in names:
            s.add(Agent(
                workspace_id=workspace_id,
                name=n,
                role="specialist",
                description=f"{n} role",
                capabilities=[],
                system_prompt=system_prompt,
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
    block = SimpleNamespace(
        type="tool_use",
        name="route_team_message",
        input={
            "summary": summary,
            "agents": [{"name": n, "reason": r} for n, r in picks],
        },
    )
    resp = SimpleNamespace(content=[block])
    fake = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: resp),
    )
    return lambda api_key: fake


def _dispatch_team_message(
    client, alice, *, content: str, picks: list[tuple[str, str]],
    summary: str = "routed.",
    monkeypatch,
) -> str:
    """Create a team conversation + post one message with the
    stubbed router. Returns the conversation_id."""
    h = auth_headers(alice["session_token"])
    monkeypatch.setattr(
        main, "_TEAM_ROUTER_ANTHROPIC_FACTORY",
        _stub_router(picks, summary=summary),
    )
    cid = client.post(
        "/workspaces/me/team-conversations", headers=h, json={},
    ).json()["id"]
    r = client.post(
        f"/team-conversations/{cid}/messages",
        headers=h, json={"content": content},
    )
    assert r.status_code == 200, r.text
    return cid


# ---------- Claim ---------- #


def test_claim_returns_oldest_pending_row_for_agent(
    client, alice, monkeypatch,
):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus", "hermes"])
    _seed_anthropic_secret(ws)
    cid = _dispatch_team_message(
        client, alice,
        content="scan + notify",
        picks=[("argus", "scan"), ("hermes", "notify")],
        monkeypatch=monkeypatch,
    )

    api = auth_headers(alice["api_key"]["plaintext"])
    r = client.post("/agents/argus/team-conversations/claim", headers=api)
    assert r.status_code == 200, r.text
    turn = r.json()["turn"]
    assert turn is not None
    assert turn["conversation_id"] == cid
    roles = [m["role"] for m in turn["messages"]]
    # User + router as system note, no peer pending rows.
    assert roles == ["user", "system"]
    assert turn["messages"][0]["content"] == "scan + notify"
    assert turn["messages"][1]["content"].startswith("Polaris routing note:")


def test_claim_only_returns_own_agents_row(
    client, alice, monkeypatch,
):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus", "hermes"])
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice,
        content="scan + notify",
        picks=[("argus", "scan"), ("hermes", "notify")],
        monkeypatch=monkeypatch,
    )

    api = auth_headers(alice["api_key"]["plaintext"])
    a = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]
    h = client.post(
        "/agents/hermes/team-conversations/claim", headers=api,
    ).json()["turn"]
    # Each agent gets its own message; ids differ.
    assert a is not None and h is not None
    assert a["message_id"] != h["message_id"]


def test_claim_flips_status_to_in_progress(
    client, alice, monkeypatch,
):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus"])
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice,
        content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )

    api = auth_headers(alice["api_key"]["plaintext"])
    mid = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]["message_id"]
    with session_scope() as s:
        assert s.get(TeamMessage, mid).status == "in_progress"

    # Second claim returns null (no more pending rows for argus).
    r2 = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    )
    assert r2.json()["turn"] is None


def test_claim_empty_returns_null(client, alice):
    api = auth_headers(alice["api_key"]["plaintext"])
    r = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    )
    assert r.status_code == 200
    assert r.json() == {"turn": None}


def test_claim_prepends_agent_system_prompt(
    client, alice, monkeypatch,
):
    ws = alice["workspace"]["id"]
    _seed_agents(
        ws, ["argus"],
        system_prompt="You are a security scanner.",
    )
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice,
        content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )

    api = auth_headers(alice["api_key"]["plaintext"])
    turn = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]
    assert turn["messages"][0] == {
        "role": "system",
        "content": "You are a security scanner.",
    }


def test_claim_does_not_cross_workspaces(
    client, alice, bob, monkeypatch,
):
    ws_a = alice["workspace"]["id"]
    _seed_agents(ws_a, ["argus"])
    _seed_anthropic_secret(ws_a)
    _dispatch_team_message(
        client, alice,
        content="alice scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )

    # Bob's workspace has no agents — claiming for "argus" under
    # bob's api key must NOT pick up alice's pending row.
    api_bob = auth_headers(bob["api_key"]["plaintext"])
    r = client.post(
        "/agents/argus/team-conversations/claim", headers=api_bob,
    )
    assert r.json()["turn"] is None


# ---------- Chunk ---------- #


def test_chunk_appends_content(client, alice, monkeypatch):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus"])
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice,
        content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )
    api = auth_headers(alice["api_key"]["plaintext"])
    mid = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]["message_id"]

    r1 = client.post(
        f"/team-messages/{mid}/chunk", headers=api,
        json={"delta": "scanning... "},
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/team-messages/{mid}/chunk", headers=api,
        json={"delta": "no secrets found."},
    )
    assert r2.json()["content"] == "scanning... no secrets found."
    assert r2.json()["status"] == "in_progress"


def test_chunk_400s_on_completed_msg(client, alice, monkeypatch):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus"])
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice, content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )
    api = auth_headers(alice["api_key"]["plaintext"])
    mid = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]["message_id"]
    client.post(
        f"/team-messages/{mid}/complete", headers=api,
        json={"content": "done"},
    )
    r = client.post(
        f"/team-messages/{mid}/chunk", headers=api,
        json={"delta": "extra"},
    )
    assert r.status_code == 400


# ---------- Complete ---------- #


def test_complete_with_content_sets_completed(
    client, alice, monkeypatch,
):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus"])
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice, content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )
    api = auth_headers(alice["api_key"]["plaintext"])
    mid = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]["message_id"]

    r = client.post(
        f"/team-messages/{mid}/complete", headers=api,
        json={"content": "all clean."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["content"] == "all clean."
    assert body["completed_at"] is not None


def test_complete_with_error_sets_error(
    client, alice, monkeypatch,
):
    ws = alice["workspace"]["id"]
    _seed_agents(ws, ["argus"])
    _seed_anthropic_secret(ws)
    _dispatch_team_message(
        client, alice, content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )
    api = auth_headers(alice["api_key"]["plaintext"])
    mid = client.post(
        "/agents/argus/team-conversations/claim", headers=api,
    ).json()["turn"]["message_id"]

    r = client.post(
        f"/team-messages/{mid}/complete", headers=api,
        json={"error": "scanner crashed"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["error"] == "scanner crashed"


def test_complete_in_another_workspace_is_404(
    client, alice, bob, monkeypatch,
):
    ws_a = alice["workspace"]["id"]
    _seed_agents(ws_a, ["argus"])
    _seed_anthropic_secret(ws_a)
    _dispatch_team_message(
        client, alice, content="scan", picks=[("argus", "scan")],
        monkeypatch=monkeypatch,
    )
    api_a = auth_headers(alice["api_key"]["plaintext"])
    mid = client.post(
        "/agents/argus/team-conversations/claim", headers=api_a,
    ).json()["turn"]["message_id"]

    api_b = auth_headers(bob["api_key"]["plaintext"])
    r = client.post(
        f"/team-messages/{mid}/complete", headers=api_b,
        json={"content": "intrusion"},
    )
    assert r.status_code == 404
