"""Phase 19.5: POST /slack/respond + Compliance preset wiring tests.

Three surfaces:

1. The new `slack:respond` capability appears in KNOWN_CAPABILITIES
   and is granted by the Compliance preset's `internal` + `public`
   hint mappings (but NOT pii / sensitive).
2. POST /slack/respond capability gate, missing-install path, happy
   path (with slack_client stubbed).
3. Slack-side errors map to 502 with a clean message.

slack_client.post_message is stubbed via monkeypatch; this test file
never hits Slack.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

import secrets_crypto
import slack_client
import zone_presets as zp
from capabilities import KNOWN_CAPABILITIES
from db import session_scope
from models import Agent, SlackChannel, SlackWorkspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Capability + preset wiring ---------- #


def test_slack_respond_is_a_known_capability():
    assert "slack:respond" in KNOWN_CAPABILITIES


def test_compliance_preset_grants_slack_respond_to_internal_and_public():
    """The Phase 19.4 orchestrator filters routing candidates on
    `slack:respond` + zone match. Compliance's internal + public hint
    mappings have to grant it automatically or the demo arc would
    require every operator to add the capability by hand."""
    internal = zp.apply_preset(zp.COMPLIANCE_TEAM, "specialist", sensitivity_hint="internal")
    public = zp.apply_preset(zp.COMPLIANCE_TEAM, "specialist", sensitivity_hint="public")
    assert "slack:respond" in internal["capabilities"]
    assert "slack:respond" in public["capabilities"]


def test_compliance_preset_withholds_slack_respond_from_pii_and_sensitive():
    """The wedge claim: pii + sensitive bots literally cannot post
    back to Slack. Even if an operator routes a mention to one of
    them (impossible under the orchestrator, but as defense-in-depth),
    the SDK's capability gate would refuse."""
    pii = zp.apply_preset(zp.COMPLIANCE_TEAM, "specialist", sensitivity_hint="pii")
    sensitive = zp.apply_preset(zp.COMPLIANCE_TEAM, "specialist", sensitivity_hint="sensitive")
    assert "slack:respond" not in pii["capabilities"]
    assert "slack:respond" not in sensitive["capabilities"]


# ---------- Endpoint tests ---------- #


def _install_slack(workspace_id: str, slack_team_id: str = "T_RESPOND") -> str:
    with session_scope() as s:
        s.add(SlackWorkspace(
            slack_team_id=slack_team_id,
            lightsei_workspace_id=workspace_id,
            team_name="Test",
            bot_token_encrypted=secrets_crypto.encrypt("xoxb-fake").encode("ascii"),
            bot_user_id="U0BOT",
            installed_at=_now(),
        ))
    return slack_team_id


def _add_channel(
    workspace_id: str,
    *,
    slack_team_id: str = "T_RESPOND",
    channel_id: str = "C_DATA",
    sensitivity_level: str = "internal",
    opted_in: bool = True,
) -> None:
    with session_scope() as s:
        s.add(SlackChannel(
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            lightsei_workspace_id=workspace_id,
            channel_name=channel_id,
            sensitivity_level=sensitivity_level,
            opted_in=opted_in,
            created_at=_now(),
            updated_at=_now(),
        ))


def _add_agent(
    workspace_id: str,
    name: str,
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


def test_respond_happy_path(client, alice, monkeypatch):
    """Agent with slack:respond + active install → endpoint calls
    slack_client.post_message + returns {ok, ts, channel}."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, channel_id="C_DATA")
    _add_agent(ws_id, "vega", ["slack:respond"])

    sent: list[dict] = []
    def _fake_post(*, session, slack_team_id, channel, text, thread_ts=None):
        sent.append({
            "slack_team_id": slack_team_id,
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
        })
        return {"ok": True, "ts": "1779200000.000100", "channel": channel}

    monkeypatch.setattr(slack_client, "post_message", _fake_post)

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "channel": "C_DATA",
            "text": "Here's the at-risk account digest…",
            "thread_ts": "1779100000.000050",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "ts": "1779200000.000100", "channel": "C_DATA"}

    # The call to post_message got the expected args.
    assert len(sent) == 1
    assert sent[0]["slack_team_id"] == "T_RESPOND"
    assert sent[0]["channel"] == "C_DATA"
    assert sent[0]["thread_ts"] == "1779100000.000050"


def test_respond_403_when_capability_missing(client, alice):
    """Agent without slack:respond → 403 capability_missing. SDK code
    catches this as LightseiCapabilityError."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, channel_id="C")
    _add_agent(ws_id, "atlas", ["internet"])  # no slack:respond

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "atlas", "channel": "C", "text": "hi"},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "capability_missing"
    assert detail["capability"] == "slack:respond"
    assert detail["agent_name"] == "atlas"
    assert detail["granted"] == ["internet"]


def test_respond_404_when_agent_unknown(client, alice):
    """source_agent doesn't exist in this workspace → 404 (not 403)
    so the SDK can distinguish "your bot isn't in this workspace"
    from "your bot lacks the capability." Different fixes."""
    _install_slack(alice["workspace"]["id"])
    _add_channel(alice["workspace"]["id"], channel_id="C")
    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "ghost-bot", "channel": "C", "text": "hi"},
    )
    assert r.status_code == 404


def test_respond_400_when_no_active_slack_install(client, alice):
    """Workspace has the agent + capability but never connected Slack.
    400 with `no_slack_install` so the dashboard can prompt the
    operator to connect Slack."""
    ws_id = alice["workspace"]["id"]
    # NOT calling _install_slack here.
    _add_agent(ws_id, "vega", ["slack:respond"])

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "channel": "C", "text": "hi"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_slack_install"


def test_respond_400_when_install_revoked(client, alice):
    """Revoked installs don't count — same 400 path as no-install."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, channel_id="C")
    with session_scope() as s:
        row = s.get(SlackWorkspace, "T_RESPOND")
        row.revoked_at = _now()
    _add_agent(ws_id, "vega", ["slack:respond"])

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "channel": "C", "text": "hi"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_slack_install"


def test_respond_502_when_slack_rejects(client, alice, monkeypatch):
    """slack_client raises SlackClientError (bot not in channel,
    channel_not_found, etc.) → 502 with clean message. Raw error in
    _debug for ops; user-facing message is generic."""
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, channel_id="C_NOPE")
    _add_agent(ws_id, "vega", ["slack:respond"])

    def _boom(**kwargs):
        raise slack_client.SlackClientError("not_in_channel")

    monkeypatch.setattr(slack_client, "post_message", _boom)

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "channel": "C_NOPE", "text": "hi"},
    )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["error"] == "slack_post_failed"
    assert "not_in_channel" in detail["_debug"]


def test_respond_403_when_channel_not_opted_in(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, channel_id="C_SILENT", opted_in=False)
    _add_agent(ws_id, "vega", ["slack:respond"])

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "channel": "C_SILENT", "text": "hi"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "slack_channel_not_allowed"


def test_respond_403_when_channel_zone_mismatch(client, alice):
    ws_id = alice["workspace"]["id"]
    _install_slack(ws_id)
    _add_channel(ws_id, channel_id="C_PUBLIC", sensitivity_level="public")
    _add_agent(ws_id, "vega", ["slack:respond"], sensitivity_level="internal")

    r = client.post(
        "/slack/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"source_agent": "vega", "channel": "C_PUBLIC", "text": "hi"},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "slack_channel_zone_mismatch"
    assert detail["agent_sensitivity_level"] == "internal"
    assert detail["channel_sensitivity_level"] == "public"


def test_respond_401_without_auth(client):
    """Endpoint requires API key auth (or session); without either,
    401 from the auth dependency."""
    r = client.post(
        "/slack/respond",
        json={"source_agent": "vega", "channel": "C", "text": "hi"},
    )
    assert r.status_code in (401, 403)  # either is acceptable for "no auth"
