"""Phase 19.6: per-channel sensitivity config endpoints.

Surfaces under test:

- `GET /workspaces/me/slack/workspaces` — list active installs.
- `GET /workspaces/me/slack/channels` — list channels filtered by
  workspace.
- `PATCH /workspaces/me/slack/channels/{slack_team_id}/{channel_id}` —
  operator sets sensitivity_level + opted_in.
- `DELETE /workspaces/me/slack/workspaces/{slack_team_id}` — revoke
  install.

The Slack `auth.revoke` upstream call is monkeypatched in the revoke
test so it doesn't hit the network. Everything else is pure DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from types import SimpleNamespace

import secrets_crypto
from db import session_scope
from models import SlackChannel, SlackWorkspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _install_slack(
    workspace_id: str,
    slack_team_id: str = "T_API_TEST",
    team_name: str = "Test Team",
    revoked: bool = False,
) -> str:
    """Insert a slack_workspaces row. Set `revoked=True` to create a
    revoked install — useful when a test wants two installs to coexist
    (the partial-unique index from 19.1 only allows ONE active install
    per Lightsei workspace, so co-existing rows must include a revoked
    one)."""
    with session_scope() as s:
        s.add(SlackWorkspace(
            slack_team_id=slack_team_id,
            lightsei_workspace_id=workspace_id,
            team_name=team_name,
            bot_token_encrypted=secrets_crypto.encrypt("xoxb-fake").encode("ascii"),
            bot_user_id="U0BOT",
            installed_at=_now(),
            revoked_at=_now() if revoked else None,
        ))
    return slack_team_id


def _add_channel(
    workspace_id: str,
    slack_team_id: str,
    channel_id: str,
    channel_name: str,
    sensitivity_level: str = "internal",
    opted_in: bool = False,
) -> None:
    with session_scope() as s:
        s.add(SlackChannel(
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            lightsei_workspace_id=workspace_id,
            channel_name=channel_name,
            sensitivity_level=sensitivity_level,
            opted_in=opted_in,
            created_at=_now(),
            updated_at=_now(),
        ))


# ---------- GET /workspaces/me/slack/workspaces ---------- #


def test_list_workspaces_returns_only_active_by_default(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id, "T_ACTIVE", team_name="Active")
    _install_slack(ws_id, "T_REVOKED", team_name="Revoked", revoked=True)

    r = client.get(
        "/workspaces/me/slack/workspaces",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    names = {w["slack_team_id"] for w in r.json()["workspaces"]}
    assert names == {"T_ACTIVE"}


def test_list_workspaces_includes_revoked_when_requested(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id, "T_A")
    _install_slack(ws_id, "T_REV", revoked=True)

    r = client.get(
        "/workspaces/me/slack/workspaces?include_revoked=true",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    names = {w["slack_team_id"] for w in r.json()["workspaces"]}
    assert names == {"T_A", "T_REV"}


def test_list_workspaces_never_returns_bot_token(client, alice):
    """Defensive: the serializer must NOT include the encrypted bot
    token. Even though it's encrypted, leaking blobs is bad form."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id, "T_TOK")
    r = client.get(
        "/workspaces/me/slack/workspaces",
        headers=auth_headers(alice["session_token"]),
    )
    body = r.json()
    for w in body["workspaces"]:
        assert "bot_token_encrypted" not in w
        assert "bot_token" not in w


def test_list_workspaces_isolates_tenants(client, alice, bob):
    """Bob's install shouldn't appear when alice lists hers."""
    _install_slack(alice["workspace"]["id"], "T_ALICE", team_name="alice-team")
    _install_slack(bob["workspace"]["id"], "T_BOB", team_name="bob-team")

    r = client.get(
        "/workspaces/me/slack/workspaces",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    names = {w["slack_team_id"] for w in r.json()["workspaces"]}
    assert names == {"T_ALICE"}


# ---------- GET /workspaces/me/slack/channels ---------- #


def test_list_channels_returns_all_for_workspace(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, "T_API_TEST", "C_DATA", "data", "internal", True)
    _add_channel(ws_id, "T_API_TEST", "C_HR", "hr", "pii", False)

    r = client.get(
        "/workspaces/me/slack/channels",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    chans = {c["channel_id"]: c for c in body["channels"]}
    assert set(chans.keys()) == {"C_DATA", "C_HR"}
    assert chans["C_DATA"]["opted_in"] is True
    assert chans["C_HR"]["sensitivity_level"] == "pii"


def test_list_channels_filters_by_slack_team_id(client, alice):
    """Realistic scenario: T_OLD was revoked but its channel rows are
    still in the DB (revoke just flips revoked_at; channels persist).
    T_NEW is the active install. The filter narrows to one team."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id, "T_OLD", revoked=True)
    _install_slack(ws_id, "T_NEW")
    _add_channel(ws_id, "T_OLD", "C_X", "x")
    _add_channel(ws_id, "T_NEW", "C_Y", "y")

    r = client.get(
        "/workspaces/me/slack/channels?slack_team_id=T_OLD",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    chans = r.json()["channels"]
    assert len(chans) == 1
    assert chans[0]["channel_id"] == "C_X"


def test_list_channels_orders_opted_in_first(client, alice):
    """Opted-in channels are the actionable ones — surface them first."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, "T_API_TEST", "C_AAA", "aaa", opted_in=False)
    _add_channel(ws_id, "T_API_TEST", "C_BBB", "bbb", opted_in=True)
    _add_channel(ws_id, "T_API_TEST", "C_CCC", "ccc", opted_in=False)

    r = client.get(
        "/workspaces/me/slack/channels",
        headers=auth_headers(alice["session_token"]),
    )
    chans = r.json()["channels"]
    assert chans[0]["channel_id"] == "C_BBB"  # opted_in=True surfaces first


# ---------- PATCH /workspaces/me/slack/channels/... ---------- #


def test_patch_channel_sets_sensitivity_and_opted_in(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, "T_API_TEST", "C_DATA", "data")

    r = client.patch(
        "/workspaces/me/slack/channels/T_API_TEST/C_DATA",
        headers=auth_headers(alice["session_token"]),
        json={"sensitivity_level": "pii", "opted_in": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sensitivity_level"] == "pii"
    assert body["opted_in"] is True

    with session_scope() as s:
        row = s.get(SlackChannel, ("T_API_TEST", "C_DATA"))
        assert row.sensitivity_level == "pii"
        assert row.opted_in is True


def test_patch_channel_can_set_just_opted_in(client, alice):
    """Both fields optional — opting a channel in shouldn't require
    re-asserting its current sensitivity."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, "T_API_TEST", "C", "c", sensitivity_level="sensitive")

    r = client.patch(
        "/workspaces/me/slack/channels/T_API_TEST/C",
        headers=auth_headers(alice["session_token"]),
        json={"opted_in": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["opted_in"] is True
    assert body["sensitivity_level"] == "sensitive"  # unchanged


def test_patch_channel_rejects_invalid_sensitivity(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, "T_API_TEST", "C", "c")

    r = client.patch(
        "/workspaces/me/slack/channels/T_API_TEST/C",
        headers=auth_headers(alice["session_token"]),
        json={"sensitivity_level": "SECRET-SQUIRREL"},
    )
    assert r.status_code == 422


def test_patch_channel_404_when_cross_tenant(client, alice, bob):
    """Bob can't change alice's channels — should 404 (not 403)
    so the existence of a channel in another tenant doesn't leak."""
    _install_slack(alice["workspace"]["id"], "T_ALICE")
    _add_channel(alice["workspace"]["id"], "T_ALICE", "C_PRIVATE", "p")

    r = client.patch(
        "/workspaces/me/slack/channels/T_ALICE/C_PRIVATE",
        headers=auth_headers(bob["session_token"]),
        json={"opted_in": True},
    )
    assert r.status_code == 404


# ---------- DELETE /workspaces/me/slack/workspaces/... ---------- #


def test_revoke_marks_workspace_revoked(client, alice, monkeypatch):
    """Sets revoked_at + best-effort calls auth.revoke. The revoke
    test stubs httpx.post so we don't hit Slack."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id, "T_TO_REVOKE")

    calls: list[dict] = []
    def _fake_post(*a, **kw):
        calls.append({"args": a, "kwargs": kw})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"ok": True},
            text="ok",
        )

    monkeypatch.setattr("httpx.post", _fake_post)

    r = client.delete(
        "/workspaces/me/slack/workspaces/T_TO_REVOKE",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["revoked_at"] is not None

    # auth.revoke called upstream.
    assert len(calls) == 1
    url = calls[0]["args"][0]
    assert "auth.revoke" in url


def test_revoke_is_idempotent(client, alice, monkeypatch):
    """Revoking a workspace twice doesn't error and doesn't call
    auth.revoke a second time (the second call would 401 anyway)."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id, "T_REV_TWICE")

    upstream_calls = []
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **kw: (
            upstream_calls.append(a) or
            SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text="ok")
        ),
    )

    r1 = client.delete(
        "/workspaces/me/slack/workspaces/T_REV_TWICE",
        headers=auth_headers(alice["session_token"]),
    )
    r2 = client.delete(
        "/workspaces/me/slack/workspaces/T_REV_TWICE",
        headers=auth_headers(alice["session_token"]),
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # auth.revoke called exactly once (on the first revoke).
    assert len(upstream_calls) == 1


def test_revoke_404_when_cross_tenant(client, alice, bob, monkeypatch):
    """Bob can't revoke alice's install."""
    _install_slack(alice["workspace"]["id"], "T_ALICE_X")
    monkeypatch.setattr("httpx.post", lambda *a, **kw: None)

    r = client.delete(
        "/workspaces/me/slack/workspaces/T_ALICE_X",
        headers=auth_headers(bob["session_token"]),
    )
    assert r.status_code == 404


def test_revoke_swallows_upstream_failure(client, alice, monkeypatch):
    """Even if Slack's auth.revoke fails (network blip, token already
    invalidated), the local revoke still succeeds — the routing-side
    state is what matters."""
    _install_slack(alice["workspace"]["id"], "T_BAD_UPSTREAM")
    import httpx
    def _boom(*a, **kw):
        raise httpx.ConnectError("upstream down")
    monkeypatch.setattr("httpx.post", _boom)

    r = client.delete(
        "/workspaces/me/slack/workspaces/T_BAD_UPSTREAM",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    assert r.json()["revoked_at"] is not None
