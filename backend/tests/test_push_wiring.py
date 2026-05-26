"""Phase 28.3: tests that POST /widget-bot/respond + POST
/workspaces/me/inbox/{id}/messages fire push notifications via
the Phase 28.2 capture mode.

Both endpoints go through the shared `_push_notify_end_user_if_subscribed`
helper in main.py. These tests verify:

  - Identified conversations + active subscription + pref='all' →
    push captured (one per active sub).
  - Anonymous conversation (conv.end_user_id is None) → no push.
  - Identified but notification_pref='off' → no push.
  - Identified but link is soft-revoked → no push.
  - No active subscriptions → push.send_to_end_user no-ops, no
    crash, response still 200.
  - Payload shape: title = vendor name, body = message preview,
    deep_link_url = /c/{slug}/conversation/{conv_id}.

Race-safe by being in capture mode (LIGHTSEI_PUSH_FAKE_CAPTURE=1).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import push
from db import session_scope
from keys import generate_session_token, hash_token
from models import (
    Agent,
    EndUser,
    EndUserPushSubscription,
    EndUserSession,
    EndUserVendorLink,
    WidgetConversation,
    Workspace,
)
from tests.conftest import auth_headers, signup


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_push_capture():
    push._reset_for_tests()
    yield
    push._reset_for_tests()


def _make_vendor(client, *, slug: str = "vendor") -> dict:
    """Operator signs up + claims slug + wires bot. Returns
    {workspace_id, session_token, slug, public_id, agent_name}."""
    # Distinct email per call so multiple vendors don't collide.
    email = f"op-{uuid.uuid4().hex[:8]}@example.com"
    sess = signup(
        client, email=email, workspace_name=f"{slug.title()} Co",
    )
    ws_id = sess["workspace"]["id"]
    op_tok = sess["session_token"]
    client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(op_tok),
        json={"slug": slug},
    )
    public_id = f"wid_{slug}_{uuid.uuid4().hex[:6]}"
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.widget_public_id = public_id
        ws.allowed_widget_origins = ["https://app.lightsei.com"]
        ws.customer_facing_agent_name = "vega"
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            description="Vendor bot.", sensitivity_level="public",
            capabilities=["widget:respond", "widget:escalate"],
            command_handlers=[],
            created_at=_now(), updated_at=_now(),
        ))
    return {
        "workspace_id": ws_id,
        "session_token": op_tok,
        "slug": slug,
        "public_id": public_id,
        "agent_name": "vega",
    }


def _make_end_user_with_link(
    workspace_id: str, *, pref: str = "all", removed: bool = False,
    with_subscription: bool = True, email: str | None = None,
) -> tuple[str, str]:
    """Returns (end_user_id, plaintext_session_token). Subscribes
    the end-user to one fake device by default."""
    euid = str(uuid.uuid4())
    tok = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid, email=email or f"eu-{euid[:8]}@example.com",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(tok),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
        s.add(EndUserVendorLink(
            end_user_id=euid,
            workspace_id=workspace_id,
            notification_pref=pref,
            removed_at=_now() if removed else None,
        ))
        if with_subscription:
            s.add(EndUserPushSubscription(
                id=str(uuid.uuid4()),
                end_user_id=euid,
                endpoint=f"https://push.example.com/{euid}",
                p256dh="BAfake-p256dh", auth="BBfake-auth",
            ))
    return euid, tok


def _start_identified_conv(
    workspace_id: str, end_user_id: str, *, agent: str = "vega",
) -> str:
    conv_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetConversation(
            id=conv_id, workspace_id=workspace_id,
            customer_facing_agent_name=agent,
            status="open",
            end_user_id=end_user_id,
            started_at=_now(), last_message_at=_now(),
        ))
    return conv_id


def _start_anon_conv(workspace_id: str) -> str:
    conv_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetConversation(
            id=conv_id, workspace_id=workspace_id,
            customer_facing_agent_name="vega",
            status="open",
            anon_user_id="anon-x",
            end_user_id=None,
            started_at=_now(), last_message_at=_now(),
        ))
    return conv_id


def _bot_respond(client, vendor, conv_id, text):
    return client.post(
        "/widget-bot/respond",
        headers={
            **auth_headers(vendor["session_token"]),
            "X-Lightsei-Agent": vendor["agent_name"],
        },
        json={
            "conversation_id": conv_id,
            "text": text,
            "source_agent": vendor["agent_name"],
        },
    )


def _operator_reply(client, vendor, conv_id, text):
    return client.post(
        f"/workspaces/me/inbox/{conv_id}/messages",
        headers=auth_headers(vendor["session_token"]),
        json={"text": text},
    )


# ---------- Bot response triggers push ---------- #


def test_bot_respond_pushes_when_identified_subscribed_and_opted_in(client):
    vendor = _make_vendor(client, slug="jyni")
    euid, _ = _make_end_user_with_link(vendor["workspace_id"])
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    r = _bot_respond(client, vendor, conv_id, "Reply from vega")
    assert r.status_code == 200

    captured = push.captured_pushes()
    assert len(captured) == 1
    p = captured[0]
    assert p["end_user_id"] == euid
    assert p["payload"]["title"] == "Jyni Co"  # vendor name
    assert p["payload"]["body"] == "Reply from vega"
    assert p["payload"]["deep_link_url"] == f"/c/jyni/conversation/{conv_id}"


def test_bot_respond_truncates_long_body_in_push_preview(client):
    vendor = _make_vendor(client, slug="halo")
    euid, _ = _make_end_user_with_link(vendor["workspace_id"])
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    long_text = "x" * 500
    r = _bot_respond(client, vendor, conv_id, long_text)
    assert r.status_code == 200

    body = push.captured_pushes()[0]["payload"]["body"]
    # 140-char preview cap from main.py.
    assert len(body) <= 140
    assert body.endswith("…")


def test_bot_respond_no_push_for_anonymous_conversation(client):
    vendor = _make_vendor(client, slug="anon-vendor")
    conv_id = _start_anon_conv(vendor["workspace_id"])

    r = _bot_respond(client, vendor, conv_id, "anonymous reply")
    assert r.status_code == 200
    assert push.captured_pushes() == []


def test_bot_respond_no_push_when_pref_off(client):
    vendor = _make_vendor(client, slug="vendor-off")
    euid, _ = _make_end_user_with_link(vendor["workspace_id"], pref="off")
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    r = _bot_respond(client, vendor, conv_id, "should-not-push")
    assert r.status_code == 200
    assert push.captured_pushes() == []


def test_bot_respond_no_push_when_link_soft_removed(client):
    vendor = _make_vendor(client, slug="vendor-revoked")
    euid, _ = _make_end_user_with_link(
        vendor["workspace_id"], removed=True,
    )
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    r = _bot_respond(client, vendor, conv_id, "should-not-push")
    assert r.status_code == 200
    assert push.captured_pushes() == []


def test_bot_respond_succeeds_when_no_subscriptions(client):
    """push.send_to_end_user no-ops when there are no subs; the
    bot-respond endpoint should not raise."""
    vendor = _make_vendor(client, slug="no-subs")
    euid, _ = _make_end_user_with_link(
        vendor["workspace_id"], with_subscription=False,
    )
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    r = _bot_respond(client, vendor, conv_id, "no-one-listening")
    assert r.status_code == 200
    assert push.captured_pushes() == []


# ---------- Operator reply triggers push ---------- #


def test_operator_reply_pushes_when_identified_subscribed_and_opted_in(client):
    vendor = _make_vendor(client, slug="op-push")
    euid, _ = _make_end_user_with_link(vendor["workspace_id"])
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    r = _operator_reply(
        client, vendor, conv_id, "Just chiming in",
    )
    assert r.status_code == 200

    captured = push.captured_pushes()
    assert len(captured) == 1
    p = captured[0]
    assert p["payload"]["title"] == "Op-Push Co"
    assert p["payload"]["body"] == "Just chiming in"
    assert p["payload"]["deep_link_url"] == f"/c/op-push/conversation/{conv_id}"


def test_operator_reply_no_push_for_anonymous(client):
    vendor = _make_vendor(client, slug="op-anon")
    conv_id = _start_anon_conv(vendor["workspace_id"])

    r = _operator_reply(client, vendor, conv_id, "anonymous side")
    assert r.status_code == 200
    assert push.captured_pushes() == []


def test_operator_reply_no_push_when_pref_off(client):
    vendor = _make_vendor(client, slug="op-off")
    euid, _ = _make_end_user_with_link(vendor["workspace_id"], pref="off")
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    r = _operator_reply(client, vendor, conv_id, "muted")
    assert r.status_code == 200
    assert push.captured_pushes() == []


# ---------- Fan-out across multiple subscriptions ---------- #


def test_push_fans_out_across_multiple_subscriptions(client):
    """End user signed in on two devices → one bot reply triggers
    two captured pushes."""
    vendor = _make_vendor(client, slug="multi-device")
    euid, _ = _make_end_user_with_link(vendor["workspace_id"])
    # Add a second subscription on a different "device".
    with session_scope() as s:
        s.add(EndUserPushSubscription(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            endpoint="https://push.example.com/second-device",
            p256dh="BBfake", auth="BBauth",
        ))
    conv_id = _start_identified_conv(vendor["workspace_id"], euid)

    _bot_respond(client, vendor, conv_id, "ping both")

    captured = push.captured_pushes()
    assert len(captured) == 2
    endpoints = {c["endpoint"] for c in captured}
    assert "https://push.example.com/second-device" in endpoints
