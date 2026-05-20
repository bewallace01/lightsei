"""Phase 19.4: chat orchestrator handler tests.

Three surfaces under test:

1. Channel auto-discovery on first event (slack_channels row inserted
   with sensitivity_level='internal', opted_in=false).
2. Gates that post a friendly Slack nudge:
   - Channel not opted in.
   - No same-zone bots with slack:respond capability.
   - LLM picks an unknown bot.
3. Happy path: routing call picks a candidate, slack.respond Command
   row is enqueued for the chosen bot.

Anthropic + Slack are both stubbed via monkeypatch — tests don't hit
either API.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy import select

import secrets_crypto
import slack_client
import slack_orchestrator
from db import session_scope
from models import (
    Agent,
    Command,
    SlackChannel,
    SlackWorkspace,
    Workspace,
    WorkspaceSecret,
)


# ---------- helpers ---------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _install_slack_workspace(workspace_id: str, slack_team_id: str = "T_ORCH") -> str:
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


def _set_anthropic_key(workspace_id: str) -> None:
    with session_scope() as s:
        s.add(WorkspaceSecret(
            workspace_id=workspace_id,
            name="ANTHROPIC_API_KEY",
            encrypted_value=secrets_crypto.encrypt("fake-anthropic-key"),
            created_at=_now(),
            updated_at=_now(),
        ))


def _opt_in_channel(
    workspace_id: str,
    slack_team_id: str,
    channel_id: str = "C_MAIN",
    *,
    sensitivity: str = "internal",
) -> None:
    with session_scope() as s:
        existing = s.get(SlackChannel, (slack_team_id, channel_id))
        if existing is None:
            s.add(SlackChannel(
                slack_team_id=slack_team_id,
                channel_id=channel_id,
                lightsei_workspace_id=workspace_id,
                channel_name="main",
                sensitivity_level=sensitivity,
                opted_in=True,
                created_at=_now(),
                updated_at=_now(),
            ))
        else:
            existing.sensitivity_level = sensitivity
            existing.opted_in = True


def _add_agent(
    workspace_id: str,
    name: str,
    sensitivity_level: str,
    capabilities: list[str],
    description: str = "",
) -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="specialist",
            description=description,
            sensitivity_level=sensitivity_level,
            capabilities=capabilities,
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))


def _make_payload(
    slack_team_id: str = "T_ORCH",
    channel_id: str = "C_MAIN",
    text: str = "@Lightsei pull our MRR",
) -> dict:
    return {
        "slack_team_id": slack_team_id,
        "channel_id": channel_id,
        "user_id": "U_CSM",
        "text": text,
        "thread_ts": None,
        "ts": "1779100000.000100",
        "slack_event_id": f"Ev_{uuid.uuid4().hex[:8]}",
    }


class _FakeAnthropicResp:
    """Stub the .content[0] tool-use block shape the orchestrator
    extracts from."""

    def __init__(self, target_agent: str, why: str = "best fit"):
        self.content = [
            SimpleNamespace(
                type="tool_use",
                name="pick_bot",
                input={"target_agent": target_agent, "why": why},
            )
        ]


def _stub_anthropic_pick(monkeypatch, target_agent: str, why: str = "best fit"):
    """Replace anthropic.Anthropic(...).messages.create with a stub that
    returns a pick_bot tool_use for `target_agent`."""
    class _Client:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(
                create=lambda **kwargs: _FakeAnthropicResp(target_agent, why)
            )

    monkeypatch.setattr("anthropic.Anthropic", _Client)


def _stub_slack_post(monkeypatch, sink: Optional[list] = None):
    """Replace slack_client.post_message with a stub. Returns the call-
    log list so tests can assert on what was posted."""
    log = sink if sink is not None else []

    def _fake(*, session, slack_team_id, channel, text, thread_ts=None):
        log.append({
            "slack_team_id": slack_team_id,
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
        })
        return {"ok": True, "ts": "stub"}

    monkeypatch.setattr(slack_client, "post_message", _fake)
    return log


# ---------- Channel auto-discovery ---------- #


def test_first_event_in_unknown_channel_inserts_silent_row(client, alice, monkeypatch):
    """First time we see a channel, insert a slack_channels row with
    safe defaults (sensitivity='internal', opted_in=false) AND post the
    not-connected nudge."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(channel_id="C_BRAND_NEW"),
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "channel_not_opted_in"

    # Row was inserted.
    with session_scope() as s:
        row = s.get(SlackChannel, ("T_ORCH", "C_BRAND_NEW"))
        assert row is not None
        assert row.sensitivity_level == "internal"
        assert row.opted_in is False

    # Nudge was posted.
    assert len(posts) == 1
    assert "not configured" in posts[0]["text"].lower()
    assert posts[0]["channel"] == "C_BRAND_NEW"


# ---------- Opt-in gate ---------- #


def test_unopted_channel_posts_nudge_and_skips(client, alice, monkeypatch):
    """An existing channel that hasn't been opted in yet gets the same
    nudge — same surface, doesn't depend on whether the row pre-exists."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    # Pre-insert the row with opted_in=false.
    with session_scope() as s:
        s.add(SlackChannel(
            slack_team_id="T_ORCH",
            channel_id="C_SILENT",
            lightsei_workspace_id=ws_id,
            channel_name="silent",
            sensitivity_level="internal",
            opted_in=False,
            created_at=_now(),
            updated_at=_now(),
        ))

    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(channel_id="C_SILENT"),
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "channel_not_opted_in"
    assert len(posts) == 1


# ---------- No-candidates path ---------- #


def test_no_same_zone_bots_posts_friendly_message(client, alice, monkeypatch):
    """Channel opted-in but no agents in the same zone with the
    slack:respond capability → posts a "no bots available" nudge."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", sensitivity="internal")
    # Bot exists but is in the wrong zone (pii) — gets filtered out.
    _add_agent(ws_id, "atlas", "pii", ["slack:respond"], description="Atlas")

    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(),
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "no_candidates"
    assert result["zone"] == "internal"
    assert len(posts) == 1
    assert "no bots are available" in posts[0]["text"].lower()


def test_zone_match_but_no_slack_capability_filters_out(client, alice, monkeypatch):
    """Bot is in the right zone but doesn't have the slack:respond
    capability — still treated as no-candidate."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", sensitivity="internal")
    _add_agent(ws_id, "vega", "internal", ["internet"], description="Vega")

    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(),
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "no_candidates"
    assert len(posts) == 1


# ---------- Happy path ---------- #


def test_dispatches_slack_respond_to_chosen_bot(client, alice, monkeypatch):
    """End-to-end: zone-matching slack-capable bot exists, routing LLM
    picks it, slack.respond Command is enqueued for that bot."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", sensitivity="internal")
    _set_anthropic_key(ws_id)
    _add_agent(
        ws_id, "atlas", "internal",
        ["slack:respond", "send_command"],
        description="Atlas: at-risk account digest",
    )
    _add_agent(
        ws_id, "vega", "internal",
        ["slack:respond", "internet"],
        description="Vega: prospect research",
    )

    _stub_anthropic_pick(monkeypatch, "atlas", why="MRR digest fits Atlas")
    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(text="@Lightsei pull our MRR"),
        )

    assert result["status"] == "dispatched"
    assert result["target_agent"] == "atlas"
    assert result["zone"] == "internal"

    # Command row inserted with the right shape.
    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert cmd is not None
        assert cmd.kind == "slack.respond"
        assert cmd.agent_name == "atlas"
        assert cmd.workspace_id == ws_id
        assert cmd.status == "pending"
        assert cmd.payload["channel_id"] == "C_MAIN"
        assert cmd.payload["slack_team_id"] == "T_ORCH"
        assert cmd.payload["text"] == "@Lightsei pull our MRR"

    # Happy path doesn't post a nudge — the bot itself will post its
    # response via lightsei.post_slack (19.5).
    assert posts == []


def test_router_picks_unknown_bot_posts_failure_nudge(client, alice, monkeypatch):
    """Defensive: if the LLM picks a bot that isn't in the candidate
    list (shouldn't happen with strict tool_choice, but possible),
    post the routing-failed nudge instead of crashing."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", sensitivity="internal")
    _set_anthropic_key(ws_id)
    _add_agent(ws_id, "atlas", "internal", ["slack:respond"], description="Atlas")

    _stub_anthropic_pick(monkeypatch, "hallucinated_bot_name")
    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(),
        )

    assert result["status"] == "failed"
    assert result["reason"] == "router_picked_unknown_bot"
    assert len(posts) == 1
    assert "couldn't figure out" in posts[0]["text"].lower()


def test_missing_anthropic_key_posts_setup_nudge(client, alice, monkeypatch):
    """Workspace hasn't set ANTHROPIC_API_KEY → tell the operator
    rather than silently dropping the mention."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", sensitivity="internal")
    _add_agent(ws_id, "atlas", "internal", ["slack:respond"])

    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(),
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "missing_anthropic_key"
    assert len(posts) == 1
    assert "anthropic" in posts[0]["text"].lower()


def test_anthropic_error_posts_routing_failed_nudge(client, alice, monkeypatch):
    """Anthropic call raises (rate-limit, 5xx, whatever) → record the
    failure on the job AND post the routing-failed nudge so the user
    knows something went wrong."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", sensitivity="internal")
    _set_anthropic_key(ws_id)
    _add_agent(ws_id, "atlas", "internal", ["slack:respond"])

    class _Boom:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(
                    RuntimeError("503 overloaded")
                )
            )

    monkeypatch.setattr("anthropic.Anthropic", _Boom)
    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(),
        )

    assert result["status"] == "failed"
    assert result["reason"] == "anthropic_error"
    assert len(posts) == 1


def test_zone_mismatch_filters_pii_bot_from_public_channel(client, alice, monkeypatch):
    """The wedge claim applied to chat: a public-zoned channel cannot
    reach a pii-zoned bot, even if the bot has slack:respond. This is
    Phase 16 enforcement on the channel boundary."""
    ws_id = alice["workspace"]["id"]
    _install_slack_workspace(ws_id)
    _opt_in_channel(ws_id, "T_ORCH", "C_PUBLIC", sensitivity="public")
    _set_anthropic_key(ws_id)
    # PII bot has slack:respond but is filtered out by zone match.
    _add_agent(ws_id, "atlas", "pii", ["slack:respond"], description="CRM bot")

    posts = _stub_slack_post(monkeypatch)

    with session_scope() as s:
        result = slack_orchestrator.run_slack_orchestration_job(
            s, ws_id, _make_payload(channel_id="C_PUBLIC"),
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "no_candidates"
    assert result["zone"] == "public"
    # Wedge confirmed: pii bot was NOT routed-to.
    assert len(posts) == 1
