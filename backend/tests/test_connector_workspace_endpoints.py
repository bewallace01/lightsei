"""Phase 20.8: operator-facing /workspaces/me/connectors endpoints.

GET /workspaces/me/connectors — list every registry connector with
its install state for the calling workspace.

DELETE /workspaces/me/connectors/{type} — revoke the active install.

httpx.post is monkeypatched in the revoke tests so they don't hit
Google's /revoke endpoint.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
import httpx

import secrets_crypto
from connectors import CONNECTOR_REGISTRY
from db import session_scope
from models import ConnectorInstallation
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _install(
    workspace_id: str,
    *,
    connector_type: str = "gmail",
    email: str = "ops@example.com",
    refresh_token: str = "rt-1",
    revoked: bool = False,
) -> str:
    """Insert a connector install. Returns install id."""
    blob = {
        "access_token": "at-1",
        "refresh_token": refresh_token,
        "expires_at": None,
    }
    install_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(ConnectorInstallation(
            id=install_id,
            workspace_id=workspace_id,
            connector_type=connector_type,
            encrypted_tokens=secrets_crypto.encrypt(json.dumps(blob)).encode("ascii"),
            scopes=["openid", "email"],
            external_account_email=email,
            installed_at=_now(),
            revoked_at=_now() if revoked else None,
        ))
    return install_id


# ---------- GET /workspaces/me/connectors ---------- #


def test_list_connectors_returns_every_registry_entry(client, alice):
    """Even with zero installs, the list returns one entry per
    connector in the registry — the dashboard's card grid needs the
    not-installed state to render Connect buttons."""
    r = client.get(
        "/workspaces/me/connectors",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    types = {c["type"] for c in body["connectors"]}
    assert types == set(CONNECTOR_REGISTRY.keys())
    # All install fields null when nothing installed.
    for c in body["connectors"]:
        assert c["install"] is None


def test_list_connectors_carries_registry_metadata(client, alice):
    """Each entry has the metadata the dashboard's card surface
    needs: display_label, oauth_provider, declared_zones,
    default_scopes, summary."""
    r = client.get(
        "/workspaces/me/connectors",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    by_type = {c["type"]: c for c in r.json()["connectors"]}
    gmail = by_type["gmail"]
    assert gmail["display_label"] == "Gmail"
    assert gmail["oauth_provider"] == "google"
    assert "public" not in gmail["declared_zones"]  # wedge invariant
    assert "internal" in gmail["declared_zones"]
    assert gmail["default_scopes"]  # non-empty
    assert gmail["summary"]  # non-empty


def test_list_connectors_surfaces_active_install(client, alice):
    """When an install exists, the `install` field carries email,
    scopes, installed_at, etc. — but NOT the encrypted_tokens."""
    ws_id = alice["workspace"]["id"]
    _install(ws_id, connector_type="gmail", email="vega@example.com")

    r = client.get(
        "/workspaces/me/connectors",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    by_type = {c["type"]: c for c in r.json()["connectors"]}
    gmail = by_type["gmail"]
    assert gmail["install"] is not None
    assert gmail["install"]["external_account_email"] == "vega@example.com"
    assert gmail["install"]["scopes"] == ["openid", "email"]
    assert "encrypted_tokens" not in gmail["install"]  # secrets never leak
    assert "access_token" not in gmail["install"]


def test_list_connectors_ignores_revoked_installs(client, alice):
    """Revoked installs should NOT surface as the active install —
    only the non-revoked one does."""
    ws_id = alice["workspace"]["id"]
    _install(ws_id, connector_type="gmail", revoked=True)

    r = client.get(
        "/workspaces/me/connectors",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    by_type = {c["type"]: c for c in r.json()["connectors"]}
    assert by_type["gmail"]["install"] is None


def test_list_connectors_isolates_workspaces(client, alice):
    """Workspace A's install never leaks into workspace B's list."""
    other_id = str(uuid.uuid4())
    from models import Workspace
    with session_scope() as s:
        s.add(Workspace(id=other_id, name="other-co", created_at=_now()))
    _install(other_id, connector_type="gmail")  # other workspace install

    r = client.get(
        "/workspaces/me/connectors",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    by_type = {c["type"]: c for c in r.json()["connectors"]}
    assert by_type["gmail"]["install"] is None


# ---------- DELETE /workspaces/me/connectors/{type} ---------- #


def test_delete_connector_revokes_active_install(client, alice, monkeypatch):
    """DELETE sets revoked_at + calls Google /revoke. Subsequent
    /workspaces/me/connectors lists show no active install."""
    ws_id = alice["workspace"]["id"]
    install_id = _install(ws_id, connector_type="gmail")

    revoke_calls: list[dict] = []
    def _fake_post(url, **kwargs):
        revoke_calls.append({"url": url, **kwargs})
        return type("R", (), {"status_code": 200, "text": "ok",
                              "json": lambda self=None: {"ok": True}})()
    monkeypatch.setattr("httpx.post", _fake_post)

    r = client.delete(
        "/workspaces/me/connectors/gmail",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "revoked"
    assert body["connector_type"] == "gmail"

    # Google /revoke called with the refresh_token.
    assert len(revoke_calls) == 1
    assert revoke_calls[0]["url"] == "https://oauth2.googleapis.com/revoke"
    assert revoke_calls[0]["data"]["token"] == "rt-1"

    # revoked_at landed in the row.
    with session_scope() as s:
        row = s.get(ConnectorInstallation, install_id)
        assert row.revoked_at is not None


def test_delete_connector_404_when_no_active_install(client, alice):
    """No install at all → 404. Same shape as no-active-only-
    revoked."""
    r = client.delete(
        "/workspaces/me/connectors/gmail",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404
    assert "gmail" in str(r.json()["detail"]).lower()


def test_delete_connector_404_when_only_revoked_install(client, alice):
    """Only revoked rows exist → still 404 — there's nothing active
    to revoke."""
    ws_id = alice["workspace"]["id"]
    _install(ws_id, connector_type="gmail", revoked=True)

    r = client.delete(
        "/workspaces/me/connectors/gmail",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


def test_delete_connector_404_for_unknown_type(client, alice):
    """Connector type not in registry → 404 before any DB work."""
    r = client.delete(
        "/workspaces/me/connectors/totally_fake",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


def test_delete_connector_isolates_workspaces(client, alice):
    """Workspace A can't revoke workspace B's install — the FK lookup
    is scoped to the calling workspace, so it surfaces as 404."""
    other_id = str(uuid.uuid4())
    from models import Workspace
    with session_scope() as s:
        s.add(Workspace(id=other_id, name="other-co-2", created_at=_now()))
    other_install = _install(other_id, connector_type="gmail")

    r = client.delete(
        "/workspaces/me/connectors/gmail",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404

    # Other workspace's install still active.
    with session_scope() as s:
        row = s.get(ConnectorInstallation, other_install)
        assert row.revoked_at is None


def test_delete_connector_survives_upstream_revoke_failure(
    client, alice, monkeypatch,
):
    """Best-effort Google /revoke: if Google rejects, the local
    revoke still succeeds. The user-facing intent is 'stop using
    this'; local revocation is what protects future bot calls."""
    ws_id = alice["workspace"]["id"]
    _install(ws_id, connector_type="gmail")

    def _failing_post(url, **kwargs):
        raise httpx.HTTPError("boom")
    monkeypatch.setattr("httpx.post", _failing_post)

    r = client.delete(
        "/workspaces/me/connectors/gmail",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    # 200 even though Google's /revoke threw.
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"


def test_reinstall_works_after_revoke(client, alice, monkeypatch):
    """Partial-unique index lets the same connector re-install after
    revoke without manual cleanup. Smoke test: revoke + insert a
    second active row + confirm it surfaces."""
    ws_id = alice["workspace"]["id"]
    _install(ws_id, connector_type="gmail", email="old@example.com")

    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: type("R", (), {
            "status_code": 200, "text": "ok",
            "json": lambda self=None: {"ok": True},
        })(),
    )
    client.delete(
        "/workspaces/me/connectors/gmail",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )

    # New install with fresh email.
    _install(ws_id, connector_type="gmail", email="new@example.com")

    r = client.get(
        "/workspaces/me/connectors",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    by_type = {c["type"]: c for c in r.json()["connectors"]}
    assert by_type["gmail"]["install"] is not None
    assert by_type["gmail"]["install"]["external_account_email"] == "new@example.com"
