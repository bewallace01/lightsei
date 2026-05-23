"""Phase 21.7: tests for /workspaces/me/widget-settings.

GET returns current config + mints public id on first call.
PATCH updates bot designation + origin allowlist, auto-grants
widget capabilities on the bot, validates origins.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from db import session_scope
from models import Agent, Workspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_agent(
    workspace_id: str,
    name: str,
    *,
    capabilities: list[str] | None = None,
    sensitivity_level: str = "public",
    description: str | None = "Customer-facing FAQ bot.",
) -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="specialist",
            sensitivity_level=sensitivity_level,
            capabilities=capabilities or [],
            command_handlers=[],
            description=description,
            created_at=_now(),
            updated_at=_now(),
        ))


# ---------- GET /workspaces/me/widget-settings ---------- #


def test_get_widget_settings_mints_public_id_on_first_call(client, alice):
    """Brand-new workspace has no widget_public_id. First GET
    mints + persists one + returns it. Second GET returns the
    same id (idempotent)."""
    r1 = client.get(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["widget_public_id"]  # non-empty
    assert body1["customer_facing_agent_name"] is None
    assert body1["allowed_widget_origins"] == []
    assert body1["available_agents"] == []  # alice has no agents

    r2 = client.get(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r2.json()["widget_public_id"] == body1["widget_public_id"]


def test_get_widget_settings_surfaces_available_agents(client, alice):
    """Each agent shows up with name + description +
    sensitivity_level + has_widget_capabilities flag. Lightsei
    system agents (`lightsei.*`) are hidden."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:respond", "widget:escalate"])
    _add_agent(ws_id, "orion", capabilities=["internet"])
    _add_agent(ws_id, "lightsei.system", description=None)

    r = client.get(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    body = r.json()
    names = [a["name"] for a in body["available_agents"]]
    assert "vega" in names
    assert "orion" in names
    assert "lightsei.system" not in names

    by_name = {a["name"]: a for a in body["available_agents"]}
    assert by_name["vega"]["has_widget_capabilities"] is True
    assert by_name["orion"]["has_widget_capabilities"] is False
    assert by_name["vega"]["description"] == "Customer-facing FAQ bot."


def test_get_widget_settings_returns_persisted_config(client, alice):
    """Pre-existing config on the workspace row surfaces verbatim."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.customer_facing_agent_name = "vega"
        ws.allowed_widget_origins = ["https://halo.dev", "https://www.halo.dev"]

    r = client.get(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    body = r.json()
    assert body["customer_facing_agent_name"] == "vega"
    assert body["allowed_widget_origins"] == [
        "https://halo.dev", "https://www.halo.dev",
    ]


# ---------- PATCH /workspaces/me/widget-settings ---------- #


def test_patch_sets_customer_facing_bot_and_auto_grants_capabilities(client, alice):
    """Picking a bot via PATCH auto-grants widget:respond +
    widget:escalate if missing. Operator gets a working bot
    without having to think about capabilities."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["internet"])

    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"customer_facing_agent_name": "vega"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer_facing_agent_name"] == "vega"

    with session_scope() as s:
        agent = s.get(Agent, (ws_id, "vega"))
        assert "widget:respond" in agent.capabilities
        assert "widget:escalate" in agent.capabilities
        # Pre-existing capability preserved.
        assert "internet" in agent.capabilities


def test_patch_clearing_bot_sets_pointer_to_null(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.customer_facing_agent_name = "vega"

    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"customer_facing_agent_name": None},
    )
    assert r.status_code == 200
    assert r.json()["customer_facing_agent_name"] is None


def test_patch_404_when_agent_not_in_workspace(client, alice):
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"customer_facing_agent_name": "does-not-exist"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "agent_not_found"


def test_patch_accepts_valid_origins(client, alice):
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"allowed_widget_origins": [
            "https://halo.dev",
            "https://www.halo.dev",
            "http://localhost:3000",  # dev origin allowed
            "https://staging.halo.dev:8080",  # port OK
        ]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowed_widget_origins"] == [
        "https://halo.dev",
        "https://www.halo.dev",
        "http://localhost:3000",
        "https://staging.halo.dev:8080",
    ]


def test_patch_dedups_and_strips_origins(client, alice):
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"allowed_widget_origins": [
            "https://halo.dev",
            "  https://halo.dev  ",  # whitespace
            "https://halo.dev",  # duplicate
            "https://www.halo.dev",
        ]},
    )
    assert r.status_code == 200
    assert r.json()["allowed_widget_origins"] == [
        "https://halo.dev", "https://www.halo.dev",
    ]


def test_patch_rejects_non_https_non_localhost_origin(client, alice):
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"allowed_widget_origins": ["http://halo.dev"]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_widget_origins"
    assert detail["errors"][0]["index"] == 0
    assert "https://" in detail["errors"][0]["error"]


def test_patch_rejects_origin_with_path(client, alice):
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"allowed_widget_origins": ["https://halo.dev/widget"]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "path" in detail["errors"][0]["error"]


def test_patch_reports_all_invalid_entries_at_once(client, alice):
    """Multiple bad entries should surface in one 422 so the
    operator can fix them all in one pass."""
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"allowed_widget_origins": [
            "https://good.example.com",
            "http://bad-scheme.example.com",
            "https://good2.example.com",
            "https://bad-path.example.com/x",
        ]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert len(detail["errors"]) == 2
    indices = sorted(e["index"] for e in detail["errors"])
    assert indices == [1, 3]


def test_patch_empty_origins_list_clears(client, alice):
    """Empty array → cleared allowlist."""
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.allowed_widget_origins = ["https://halo.dev"]

    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"allowed_widget_origins": []},
    )
    assert r.status_code == 200
    assert r.json()["allowed_widget_origins"] == []


def test_patch_requires_at_least_one_field(client, alice):
    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={},
    )
    assert r.status_code == 422


def test_patch_tenant_isolation(client, alice):
    """Workspace A's PATCH can't designate workspace B's bot."""
    other_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=other_id, name="other-co", created_at=_now()))
    _add_agent(other_id, "other-bot")

    r = client.patch(
        "/workspaces/me/widget-settings",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"customer_facing_agent_name": "other-bot"},
    )
    assert r.status_code == 404
