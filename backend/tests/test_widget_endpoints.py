"""Phase 21.2: tests for the public widget chat surface.

Three endpoints:

  POST /widget/{public_id}/messages
  GET  /widget/{public_id}/conversations/{conversation_id}
  GET  /widget/{public_id}/config

Cover surface areas:

- Workspace lookup by public id (404 on miss).
- Origin allowlist enforcement (403 on mismatch / missing).
- Rate limiting (per-conversation + per-workspace).
- New-conversation flow + append-to-existing flow.
- Conversation isolation across workspaces.
- Operator-owned status pauses orchestrator job enqueue.
- Poll endpoint cursor + thread ordering.
- Config endpoint shape + non-leakage.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from db import session_scope
from end_user_auth import hash_token
from datetime import timedelta
from models import (
    Agent,
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    GenerationJob,
    Workspace,
    WidgetConversation,
    WidgetMessage,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_widget_workspace(
    *,
    public_id: str = "wid_test_42",
    allowed_origins: list[str] | None = None,
    customer_facing_agent_name: str | None = "vega",
) -> str:
    """Create a workspace with a widget public id + an active bot
    designated as customer-facing. Returns the workspace id."""
    ws_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(
            id=ws_id,
            name=f"widget-co-{ws_id[:8]}",
            created_at=_now(),
            widget_public_id=public_id,
            allowed_widget_origins=allowed_origins or ["https://customer.example.com"],
            customer_facing_agent_name=customer_facing_agent_name,
        ))
        s.flush()
        if customer_facing_agent_name:
            s.add(Agent(
                workspace_id=ws_id,
                name=customer_facing_agent_name,
                role="specialist",
                description="A friendly product-FAQ bot for Customer Co.",
                sensitivity_level="public",
                capabilities=["widget:respond", "widget:escalate"],
                command_handlers=[],
                created_at=_now(),
                updated_at=_now(),
            ))
    return ws_id


def _origin() -> dict[str, str]:
    return {"origin": "https://customer.example.com"}


# ---------- POST /widget/{public_id}/messages — happy + sad paths ---------- #


def test_post_message_404_unknown_public_id(client):
    """Public id not in the database → 404. Same shape no matter
    why (operator rotated id, customer pasted the wrong snippet)."""
    r = client.post(
        "/widget/wid_does_not_exist/messages",
        headers=_origin(),
        json={"text": "hi"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "widget_not_found"


def test_post_message_403_origin_missing(client):
    """No Origin header → 403. Widget iframes always send one;
    bare-internet POSTs need to set it explicitly."""
    _make_widget_workspace()
    r = client.post(
        "/widget/wid_test_42/messages",
        json={"text": "hi"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "widget_origin_missing"


def test_post_message_403_origin_not_allowed(client):
    """Origin not in allowlist → 403 with the offending origin
    echoed back."""
    _make_widget_workspace()
    r = client.post(
        "/widget/wid_test_42/messages",
        headers={"origin": "https://attacker.example.org"},
        json={"text": "hi"},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "widget_origin_not_allowed"
    assert detail["origin"] == "https://attacker.example.org"


def test_post_message_503_when_no_customer_facing_bot(client):
    """No customer_facing_agent_name set → 503 widget_unconfigured.
    Surface the unconfigured state cleanly rather than enqueueing
    a doomed job."""
    _make_widget_workspace(customer_facing_agent_name=None)
    r = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "hi"},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "widget_unconfigured"


def test_post_message_starts_new_conversation(client):
    """No conversation_id → opens a new conversation, persists the
    user message, returns the conversation id + message id +
    job id, status 202."""
    ws_id = _make_widget_workspace()
    r = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "How do I cancel my subscription?", "anon_user_id": "u_42"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["conversation_id"]
    assert body["message_id"]
    assert body["job_id"]  # orchestrator job enqueued

    with session_scope() as s:
        conv = s.get(WidgetConversation, body["conversation_id"])
        assert conv is not None
        assert conv.workspace_id == ws_id
        assert conv.customer_facing_agent_name == "vega"
        assert conv.status == "open"
        assert conv.anon_user_id == "u_42"

        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv.id
            )
        ).scalars().all()
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].text == "How do I cancel my subscription?"


def test_post_message_appends_to_existing_conversation(client):
    """Second message in the same conversation appends rather than
    starting a fresh thread."""
    _make_widget_workspace()
    r1 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "first"},
    )
    conv_id = r1.json()["conversation_id"]

    r2 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "second", "conversation_id": conv_id},
    )
    assert r2.status_code == 202
    assert r2.json()["conversation_id"] == conv_id

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id)
        ).scalars().all()
        assert [m.text for m in msgs] == ["first", "second"]


def test_post_message_enqueues_widget_chat_job(client):
    """Successful message lands a `widget_chat` row on
    generation_jobs for the 21.6 orchestrator to pick up."""
    ws_id = _make_widget_workspace()
    r = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "hello"},
    )
    job_id = r.json()["job_id"]

    with session_scope() as s:
        job = s.get(GenerationJob, job_id)
        assert job is not None
        assert job.workspace_id == ws_id
        assert job.kind == "widget_chat"
        assert job.status == "pending"
        assert job.request_payload["conversation_id"] == r.json()["conversation_id"]
        assert job.request_payload["user_message_id"] == r.json()["message_id"]


def test_post_message_404_unknown_conversation_id(client):
    """Conversation id that doesn't exist → 404. Same shape for
    foreign-workspace conversation ids (defense-in-depth)."""
    _make_widget_workspace()
    r = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "hi", "conversation_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "conversation_not_found"


def test_post_message_404_when_conversation_belongs_to_other_workspace(client):
    """A leaked conversation id from workspace A can't be used to
    post into workspace B's conversation. Tenant isolation."""
    _make_widget_workspace(public_id="wid_a")
    other_ws_id = _make_widget_workspace(public_id="wid_b")
    # Open a conversation on workspace B.
    r_open = client.post(
        "/widget/wid_b/messages",
        headers=_origin(),
        json={"text": "B opened"},
    )
    leaked_conv_id = r_open.json()["conversation_id"]

    # Try to post into it from workspace A's public id.
    r = client.post(
        "/widget/wid_a/messages",
        headers=_origin(),
        json={"text": "cross-ws", "conversation_id": leaked_conv_id},
    )
    assert r.status_code == 404


def test_post_message_operator_owned_skips_orchestrator(client):
    """If a conversation is in operator_owned status (the operator
    clicked "Take Over" in /inbox), the user's message is still
    recorded but no orchestrator job is enqueued — the bot is
    paused."""
    _make_widget_workspace()
    # Start a conversation, then flip it to operator_owned.
    r1 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "first"},
    )
    conv_id = r1.json()["conversation_id"]
    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        conv.status = "operator_owned"

    r2 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "while operator-owned", "conversation_id": conv_id},
    )
    assert r2.status_code == 202
    assert r2.json()["job_id"] is None  # bot paused

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalars().all()
        # User message recorded — operator sees it in /inbox even
        # though the bot didn't run.
        assert len(msgs) == 2


# ---------- Rate limits ---------- #


def test_post_message_per_conversation_rate_limit(client):
    """Three rapid posts on the same conversation_id within the
    1-msg/sec window → 429 on the third.

    First POST has no conversation_id (server mints one) so it
    skips the per-conv key. Second POST records the conv-id key's
    first hit (allowed at limit=1). Third POST trips the limit.
    """
    _make_widget_workspace()
    r1 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "first"},
    )
    assert r1.status_code == 202
    conv_id = r1.json()["conversation_id"]

    r2 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "second", "conversation_id": conv_id},
    )
    assert r2.status_code == 202

    r3 = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "third", "conversation_id": conv_id},
    )
    assert r3.status_code == 429


# ---------- GET /widget/{public_id}/conversations/{conversation_id} ---------- #


def test_get_conversation_returns_messages(client):
    """Poll returns all messages on the conversation when no `since`
    cursor is supplied."""
    _make_widget_workspace()
    r_open = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "hello"},
    )
    conv_id = r_open.json()["conversation_id"]

    r = client.get(
        f"/widget/wid_test_42/conversations/{conv_id}",
        headers=_origin(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conversation_id"] == conv_id
    assert body["status"] == "open"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["text"] == "hello"


def test_get_conversation_respects_since_cursor(client):
    """`?since=<msg_id>` returns only messages with id > cursor.
    Drives the widget's poll-for-new shape."""
    _make_widget_workspace()
    r_open = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "first"},
    )
    conv_id = r_open.json()["conversation_id"]
    first_msg_id = r_open.json()["message_id"]

    # Insert a second message directly (simulating the bot's reply
    # that 21.6 will write).
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="bot",
            text="how can I help?",
            sent_at=_now(),
        ))

    r = client.get(
        f"/widget/wid_test_42/conversations/{conv_id}",
        headers=_origin(),
        params={"since": first_msg_id},
    )
    body = r.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "bot"


def test_get_conversation_404_unknown_id(client):
    _make_widget_workspace()
    r = client.get(
        f"/widget/wid_test_42/conversations/{uuid.uuid4()}",
        headers=_origin(),
    )
    assert r.status_code == 404


def test_get_conversation_origin_enforcement(client):
    """GET endpoint also enforces Origin allowlist — defense
    against cross-origin enumeration."""
    _make_widget_workspace()
    r_open = client.post(
        "/widget/wid_test_42/messages",
        headers=_origin(),
        json={"text": "hi"},
    )
    conv_id = r_open.json()["conversation_id"]

    r = client.get(
        f"/widget/wid_test_42/conversations/{conv_id}",
        headers={"origin": "https://attacker.example.org"},
    )
    assert r.status_code == 403


# ---------- GET /widget/{public_id}/config ---------- #


def test_config_returns_safe_fields(client):
    """Config returns the bot's display name + description +
    sensitivity_level. Does NOT leak workspace id, operator email,
    capability list, or system prompt."""
    _make_widget_workspace()
    r = client.get(
        "/widget/wid_test_42/config",
        headers=_origin(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["public_id"] == "wid_test_42"
    assert body["anonymous"] is True
    # Customer-facing display name (never the raw id): "vega" -> "Vega".
    assert body["bot"]["name"] == "Vega"
    assert body["bot"]["sensitivity_level"] == "public"
    # Branding defaults to null when the owner hasn't customized; the
    # "Powered by" badge shows by default.
    assert body["branding"] == {
        "title": None, "accent_color": None, "greeting": None,
        "show_powered_by": True,
    }

    # Safety: explicitly verify nothing extra leaked.
    flat = str(body)
    assert "workspace_id" not in flat
    assert "capabilities" not in flat
    assert "system_prompt" not in flat


def test_config_returns_owner_branding(client):
    ws_id = _make_widget_workspace()
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.widget_title = "Ava"
        ws.widget_accent_color = "#16a34a"
        ws.widget_greeting = "Hi! How can I help?"

    body = client.get("/widget/wid_test_42/config", headers=_origin()).json()
    assert body["branding"]["title"] == "Ava"
    assert body["branding"]["accent_color"] == "#16a34a"
    assert body["branding"]["greeting"] == "Hi! How can I help?"


def test_config_404_unknown_public_id(client):
    r = client.get(
        "/widget/wid_does_not_exist/config",
        headers=_origin(),
    )
    assert r.status_code == 404


def test_config_origin_enforcement(client):
    """Config endpoint also blocks unknown origins — prevents
    cross-internet enumeration of which workspaces have widgets."""
    _make_widget_workspace()
    r = client.get(
        "/widget/wid_test_42/config",
        headers={"origin": "https://attacker.example.org"},
    )
    assert r.status_code == 403


def test_config_bot_null_when_agent_deleted(client):
    """If the operator picks a bot then later deletes the agent
    row, config returns `bot: null` rather than erroring — the
    iframe still renders, just without a bot name."""
    _make_widget_workspace()
    # Delete the agent row that was set as customer-facing.
    with session_scope() as s:
        s.execute(text(
            "DELETE FROM agents WHERE name = 'vega'"
        ))

    r = client.get(
        "/widget/wid_test_42/config",
        headers=_origin(),
    )
    assert r.status_code == 200
    assert r.json()["bot"] is None


# ---------- Phase 31.x: authenticated-bearer bypass for Origin ---------- #
#
# The Origin allowlist exists to defend the anonymous iframe path
# against CSRF and embedding-from-untrusted-sites. A request that
# carries a valid end-user bearer token (native iOS app, web /c page)
# is by definition a first-party API client where that threat model
# doesn't apply — bearer auth is itself anti-CSRF. These tests pin
# the bypass behavior so a future refactor can't silently re-tighten
# the check and break the iOS chat surface.


def _seed_end_user_with_link(workspace_id: str) -> str:
    """Create an end-user + active workspace link + session.
    Returns the bearer token string."""
    token = f"eust_test_{uuid.uuid4().hex}"
    euid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(EndUser(
            id=euid,
            email=f"bypass-{euid[:8]}@example.com",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
        s.add(EndUserVendorLink(
            end_user_id=euid, workspace_id=workspace_id,
        ))
    return token


def test_post_message_bearer_bypasses_missing_origin(client):
    """End-user bearer + NO Origin header → 200 (the iOS app case).
    Anonymous + NO Origin → 403 (the existing test_post_message_403_
    origin_missing case). The presence of the bearer is what flips
    the gate."""
    ws_id = _make_widget_workspace()
    token = _seed_end_user_with_link(ws_id)
    r = client.post(
        "/widget/wid_test_42/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "hi from iOS app"},
    )
    assert r.status_code == 202, r.text


def test_post_message_bearer_bypasses_disallowed_origin(client):
    """End-user bearer + Origin not in allowlist → 200. Confirms the
    bypass isn't just for missing-Origin; any first-party authed
    request skips the check (iOS apps may send any Origin, none, or
    a sentinel — they all should work)."""
    ws_id = _make_widget_workspace()
    token = _seed_end_user_with_link(ws_id)
    r = client.post(
        "/widget/wid_test_42/messages",
        headers={
            "Authorization": f"Bearer {token}",
            "origin": "https://attacker.example.org",
        },
        json={"text": "hi"},
    )
    assert r.status_code == 202, r.text


def test_get_conversation_bearer_bypasses_missing_origin(client):
    """GET poll endpoint: end-user bearer + NO Origin → 200 (or
    404 since the conversation doesn't exist, but NOT 403). Anything
    other than 403 confirms the Origin check was skipped."""
    ws_id = _make_widget_workspace()
    token = _seed_end_user_with_link(ws_id)
    fake_conv_id = str(uuid.uuid4())
    r = client.get(
        f"/widget/wid_test_42/conversations/{fake_conv_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Either 200 (if the polling logic returns an empty thread) or
    # 404 (unknown conversation) is fine. 403 would mean Origin
    # bypass failed.
    assert r.status_code in (200, 404), r.text


def test_post_message_anonymous_missing_origin_still_403(client):
    """Regression: removing the bearer must still 403 on missing
    Origin. The 25.4 tests still cover the anonymous-with-Origin
    happy path; this pins the negative case."""
    _make_widget_workspace()
    r = client.post(
        "/widget/wid_test_42/messages",
        json={"text": "hi"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "widget_origin_missing"


# ---------- Phase 36.2: "Powered by" badge gating ---------- #


def test_config_branding_shown_on_free_even_if_hidden(client):
    """Free plan always shows the badge, even with hide_branding set."""
    ws_id = _make_widget_workspace()
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.plan_tier = "free"
        ws.widget_hide_branding = True

    body = client.get("/widget/wid_test_42/config", headers=_origin()).json()
    assert body["branding"]["show_powered_by"] is True


def test_config_branding_hidden_on_paid_when_opted_out(client):
    ws_id = _make_widget_workspace()
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.plan_tier = "paid"
        ws.widget_hide_branding = True

    body = client.get("/widget/wid_test_42/config", headers=_origin()).json()
    assert body["branding"]["show_powered_by"] is False


def test_config_branding_shown_on_paid_by_default(client):
    ws_id = _make_widget_workspace()
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.plan_tier = "paid"
        ws.widget_hide_branding = False

    body = client.get("/widget/wid_test_42/config", headers=_origin()).json()
    assert body["branding"]["show_powered_by"] is True
