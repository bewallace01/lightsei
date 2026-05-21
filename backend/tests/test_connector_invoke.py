"""Phase 20.6: POST /connectors/{type}/{tool} endpoint tests.

Capability gate, zone gate, install lookup, token-refresh-on-401-
then-retry, and the Run + Event row that records the call. All
connector INVOKE + Google OAuth refresh calls are monkeypatched so
the tests never hit Google.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select

import connectors as connectors_pkg
import secrets_crypto
from connectors import (
    CONNECTOR_REGISTRY,
    ConnectorAuthExpired,
    ConnectorCallError,
)
from connectors import google_oauth as connector_google_oauth
from db import session_scope
from models import Agent, ConnectorInstallation, Event, Run
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _install_connector(
    workspace_id: str,
    *,
    connector_type: str = "gmail",
    access_token: str = "at-fresh",
    refresh_token: str | None = "rt-still-valid",
    revoked: bool = False,
) -> str:
    """Insert a connector install with a known token blob. Returns
    the install id."""
    blob = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": None,
    }
    encrypted = secrets_crypto.encrypt(json.dumps(blob)).encode("ascii")
    install_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(ConnectorInstallation(
            id=install_id,
            workspace_id=workspace_id,
            connector_type=connector_type,
            encrypted_tokens=encrypted,
            scopes=["openid", "email"],
            external_account_email="ops@example.com",
            installed_at=_now(),
            revoked_at=_now() if revoked else None,
        ))
    return install_id


def _add_agent(
    workspace_id: str,
    name: str,
    *,
    capabilities: list[str],
    sensitivity_level: str = "internal",
) -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="specialist",
            sensitivity_level=sensitivity_level,
            capabilities=capabilities,
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))


def _stub_invoke(monkeypatch, connector_type: str, fn):
    """Monkeypatch the connector's INVOKE function. Each call to the
    endpoint dispatches via `spec.invoke(...)` which (in the registry)
    resolves to a lazy lookup of the per-connector module's INVOKE;
    patching the registry entry's `invoke` directly is the cleanest
    way to stub it for a test.

    The ConnectorSpec is frozen (dataclass(frozen=True)), so we
    monkeypatch the whole dict entry to a copy with the new invoke.
    """
    from dataclasses import replace

    spec = CONNECTOR_REGISTRY[connector_type]
    patched = replace(spec, invoke=fn)
    monkeypatch.setitem(CONNECTOR_REGISTRY, connector_type, patched)


# ---------- Registry + agent lookups ---------- #


def test_invoke_404_unknown_connector(client, alice):
    """Unknown connector_type → 404 unknown_connector."""
    r = client.post(
        "/connectors/totally_fake/list_files",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_connector"
    assert r.json()["detail"]["connector_type"] == "totally_fake"


def test_invoke_404_unknown_agent(client, alice):
    """source_agent doesn't exist → 404, BEFORE capability check
    (different error class from missing-capability)."""
    _install_connector(alice["workspace"]["id"])
    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "ghost-bot", "payload": {}},
    )
    assert r.status_code == 404


# ---------- Capability gate ---------- #


def test_invoke_403_capability_missing(client, alice):
    """Agent without `connector:gmail` capability → 403."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "atlas", capabilities=["internet"])
    _install_connector(ws_id)

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "atlas", "payload": {}},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "capability_missing"
    assert detail["capability"] == "connector:gmail"
    assert detail["agent_name"] == "atlas"
    assert detail["granted"] == ["internet"]


# ---------- Zone gate ---------- #


def test_invoke_403_zone_mismatch(client, alice):
    """Gmail's declared_zones excludes 'public'. A public-zoned bot
    with the capability still gets refused — the wedge invariant."""
    ws_id = alice["workspace"]["id"]
    _add_agent(
        ws_id, "researcher",
        capabilities=["connector:gmail", "internet"],
        sensitivity_level="public",
    )
    _install_connector(ws_id)

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "researcher", "payload": {}},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "connector_zone_mismatch"
    assert detail["connector_type"] == "gmail"
    assert detail["agent_sensitivity_level"] == "public"
    assert "public" not in detail["declared_zones"]


def test_invoke_calendar_allows_public_zone(client, alice, monkeypatch):
    """Calendar's declared_zones includes 'public' — scheduling
    spans every zone. So a public-zoned bot with the capability
    DOES get through."""
    ws_id = alice["workspace"]["id"]
    _add_agent(
        ws_id, "scheduler",
        capabilities=["connector:google_calendar"],
        sensitivity_level="public",
    )
    _install_connector(ws_id, connector_type="google_calendar")
    _stub_invoke(monkeypatch, "google_calendar",
                 lambda **kw: {"events": []})

    r = client.post(
        "/connectors/google_calendar/list_events",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "scheduler", "payload": {}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# ---------- Install lookup ---------- #


def test_invoke_400_when_no_install(client, alice):
    """Capability + zone OK, but no install for this connector_type
    → 400 connector_not_installed."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    # NOT calling _install_connector.

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "connector_not_installed"


def test_invoke_400_when_install_revoked(client, alice):
    """Revoked install doesn't count — same 400 path as no install."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id, revoked=True)

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "connector_not_installed"


# ---------- Happy path ---------- #


def test_invoke_happy_path_returns_result(client, alice, monkeypatch):
    """Gates pass → INVOKE called with the right access_token + the
    tool-name from the URL + the payload from the body. Result is
    returned under {ok: True, result: ...}."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id, access_token="at-fresh")

    seen_args: list[dict] = []
    def _fake_invoke(*, tool_name, payload, access_token):
        seen_args.append({
            "tool_name": tool_name,
            "payload": payload,
            "access_token": access_token,
        })
        return {"labels": [{"id": "INBOX"}]}

    _stub_invoke(monkeypatch, "gmail", _fake_invoke)

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {"foo": "bar"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"] == {"labels": [{"id": "INBOX"}]}

    assert len(seen_args) == 1
    assert seen_args[0]["tool_name"] == "list_labels"
    assert seen_args[0]["payload"] == {"foo": "bar"}
    assert seen_args[0]["access_token"] == "at-fresh"


# ---------- Refresh-then-retry ---------- #


def test_invoke_refreshes_on_401_then_retries(client, alice, monkeypatch):
    """First INVOKE call raises ConnectorAuthExpired → endpoint
    refreshes the access_token via google_oauth.refresh_access_token,
    persists the new encrypted blob, and retries the INVOKE once with
    the fresh token."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    install_id = _install_connector(
        ws_id, access_token="at-expired", refresh_token="rt-good",
    )

    call_count = {"n": 0}
    tokens_seen: list[str] = []
    def _flaky_invoke(*, tool_name, payload, access_token):
        call_count["n"] += 1
        tokens_seen.append(access_token)
        if call_count["n"] == 1:
            raise ConnectorAuthExpired("401 from upstream")
        return {"ok_after_refresh": True}

    _stub_invoke(monkeypatch, "gmail", _flaky_invoke)

    def _fake_refresh(*, refresh_token: str) -> dict:
        assert refresh_token == "rt-good"
        return {
            "access_token": "at-fresh",
            "refresh_token": None,  # Google omits → keep existing
            "expires_in": 3600,
            "scope": "openid email",
        }

    monkeypatch.setattr(
        connector_google_oauth, "refresh_access_token", _fake_refresh,
    )

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"ok_after_refresh": True}

    # Two INVOKE calls, second one with the refreshed token.
    assert call_count["n"] == 2
    assert tokens_seen == ["at-expired", "at-fresh"]

    # New blob persisted: access_token rotated, refresh_token kept.
    with session_scope() as s:
        row = s.get(ConnectorInstallation, install_id)
        blob = json.loads(secrets_crypto.decrypt(bytes(row.encrypted_tokens)))
        assert blob["access_token"] == "at-fresh"
        assert blob["refresh_token"] == "rt-good"


def test_invoke_502_when_refresh_fails(client, alice, monkeypatch):
    """refresh_access_token raises GoogleConnectorOAuthError
    (invalid_grant etc.) → 502 connector_auth_failed. Install isn't
    auto-revoked — operator decides whether to reinstall."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id, refresh_token="rt-revoked")

    _stub_invoke(monkeypatch, "gmail",
                 lambda **kw: (_ for _ in ()).throw(
                     ConnectorAuthExpired("401")))

    def _boom_refresh(**kw):
        raise connector_google_oauth.GoogleConnectorOAuthError(
            "invalid_grant"
        )
    monkeypatch.setattr(
        connector_google_oauth, "refresh_access_token", _boom_refresh,
    )

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "connector_auth_failed"


def test_invoke_502_when_second_401_after_refresh(client, alice, monkeypatch):
    """If the upstream returns 401 even AFTER a successful refresh,
    don't loop. 502 connector_auth_failed."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id, refresh_token="rt-good")

    def _always_401(**kw):
        raise ConnectorAuthExpired("401 again")
    _stub_invoke(monkeypatch, "gmail", _always_401)

    monkeypatch.setattr(
        connector_google_oauth, "refresh_access_token",
        lambda **kw: {
            "access_token": "at-fresh", "refresh_token": None,
            "expires_in": 3600, "scope": "",
        },
    )

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "connector_auth_failed"


def test_invoke_502_when_no_refresh_token_on_file(client, alice, monkeypatch):
    """Upstream 401 + no refresh_token stored → 502 (defensive; the
    install path requires refresh_token, but stale rows might lack
    it). Should NOT call refresh_access_token."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id, refresh_token=None)

    _stub_invoke(monkeypatch, "gmail",
                 lambda **kw: (_ for _ in ()).throw(
                     ConnectorAuthExpired("401")))

    refresh_called = {"n": 0}
    def _refresh(**kw):
        refresh_called["n"] += 1
        return {"access_token": "x", "expires_in": 60, "scope": ""}
    monkeypatch.setattr(
        connector_google_oauth, "refresh_access_token", _refresh,
    )

    r = client.post(
        "/connectors/gmail/list_labels",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "payload": {}},
    )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "connector_auth_failed"
    assert refresh_called["n"] == 0


# ---------- ConnectorCallError → 502 ---------- #


def test_invoke_502_on_connector_call_error(client, alice, monkeypatch):
    """Non-auth upstream error → 502 connector_call_failed. Carries
    upstream_status + error message in _debug."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id)

    def _bad_upstream(**kw):
        raise ConnectorCallError("gmail 429 rate limit",
                                 upstream_status=429)
    _stub_invoke(monkeypatch, "gmail", _bad_upstream)

    r = client.post(
        "/connectors/gmail/send_email",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega",
              "payload": {"to": "a@b.co", "subject": "s", "body": "b"}},
    )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["error"] == "connector_call_failed"
    assert detail["connector_type"] == "gmail"
    assert detail["tool_name"] == "send_email"
    assert detail["_debug"]["upstream_status"] == 429


# ---------- Run + Event recording ---------- #


def test_invoke_records_run_and_event_on_success(client, alice, monkeypatch):
    """Successful call drops a Run + a `connector_call_completed`
    Event row. Run carries the agent's sensitivity_level snapshot
    so zone-rollups stay historically correct."""
    ws_id = alice["workspace"]["id"]
    _add_agent(
        ws_id, "vega",
        capabilities=["connector:gmail"],
        sensitivity_level="sensitive",
    )
    _install_connector(ws_id)
    _stub_invoke(monkeypatch, "gmail", lambda **kw: {"id": "MSG_1"})

    r = client.post(
        "/connectors/gmail/send_email",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega",
              "payload": {"to": "a@b.co", "subject": "s", "body": "b"}},
    )
    assert r.status_code == 200, r.text

    with session_scope() as s:
        runs = s.execute(
            select(Run).where(
                Run.workspace_id == ws_id, Run.agent_name == "vega",
            )
        ).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.sensitivity_level == "sensitive"
        assert run.ended_at is not None  # synchronous: started_at == ended_at

        events = s.execute(
            select(Event).where(
                Event.workspace_id == ws_id,
                Event.run_id == run.id,
            )
        ).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "connector_call_completed"
        assert ev.payload["connector_type"] == "gmail"
        assert ev.payload["tool_name"] == "send_email"
        assert ev.payload["ok"] is True


def test_invoke_records_run_and_event_on_failure(client, alice, monkeypatch):
    """ConnectorCallError → 502, but the Run + a
    `connector_call_failed` Event still get recorded for ops."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["connector:gmail"])
    _install_connector(ws_id)

    def _bad(**kw):
        raise ConnectorCallError("gmail 503", upstream_status=503)
    _stub_invoke(monkeypatch, "gmail", _bad)

    r = client.post(
        "/connectors/gmail/send_email",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega",
              "payload": {"to": "a@b.co", "subject": "s", "body": "b"}},
    )
    assert r.status_code == 502

    with session_scope() as s:
        events = s.execute(
            select(Event).where(
                Event.workspace_id == ws_id,
                Event.kind == "connector_call_failed",
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].payload["upstream_status"] == 503
        assert events[0].payload["ok"] is False
