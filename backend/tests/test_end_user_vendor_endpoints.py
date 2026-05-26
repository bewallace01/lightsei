"""Phase 26.2: tests for the end-user-authed vendor + conversation
endpoints.

Three endpoints under test:

  GET /me/end-user
  GET /me/end-user/vendors/{slug}
  GET /me/end-user/vendors/{slug}/conversations

Auth via the `get_end_user` dep from Phase 25.3; the cross-token-type
+ expired/revoked paths are covered by `test_end_user_auth.py`, so
these tests focus on response shape, linking visibility, and
conversation scoping.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from keys import generate_session_token, hash_token
from models import (
    Agent,
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    WidgetConversation,
    Workspace,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_vendor(
    *, slug: str | None = "demo",
    public_id: str | None = None,
    agent_name: str = "vega",
) -> str:
    """Spin up a workspace + vendor_slug + customer-facing bot.
    Returns workspace_id."""
    ws_id = str(uuid.uuid4())
    public_id = public_id or f"wid_{ws_id[:8]}"
    with session_scope() as s:
        s.add(Workspace(
            id=ws_id,
            name=f"vendor-{ws_id[:8]}",
            created_at=_now(),
            vendor_slug=slug,
            widget_public_id=public_id,
            customer_facing_agent_name=agent_name,
        ))
        s.flush()
        s.add(Agent(
            workspace_id=ws_id,
            name=agent_name,
            role="specialist",
            description="bot",
            sensitivity_level="public",
            capabilities=["widget:respond"],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))
    return ws_id


def _make_end_user(*, email: str | None = None) -> tuple[str, str]:
    """Returns (end_user_id, plaintext_session_token)."""
    euid = str(uuid.uuid4())
    token = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid,
            email=email or f"eu-{euid[:8]}@example.com",
            display_name="Alice",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
    return euid, token


def _link(end_user_id: str, workspace_id: str) -> None:
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=end_user_id, workspace_id=workspace_id,
        ))


def _start_conv(workspace_id: str, end_user_id: str, *, agent: str = "vega") -> str:
    conv_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetConversation(
            id=conv_id,
            workspace_id=workspace_id,
            customer_facing_agent_name=agent,
            status="open",
            end_user_id=end_user_id,
            started_at=_now(),
            last_message_at=_now(),
        ))
    return conv_id


def _eu_auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------- GET /me/end-user ---------- #


def test_me_end_user_requires_auth(client):
    r = client.get("/me/end-user")
    assert r.status_code == 401


def test_me_end_user_returns_profile_and_no_linked_vendors_initially(client):
    _, token = _make_end_user(email="profile@example.com")
    r = client.get("/me/end-user", headers=_eu_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["end_user"]["email"] == "profile@example.com"
    assert body["end_user"]["email_verified"] in (True, False)
    assert body["linked_vendors"] == []


def test_me_end_user_includes_linked_vendor_with_trimmed_fields(client):
    ws_id = _make_vendor(slug="jyni")
    euid, token = _make_end_user()
    _link(euid, ws_id)

    r = client.get("/me/end-user", headers=_eu_auth(token))
    assert r.status_code == 200
    vendors = r.json()["linked_vendors"]
    assert len(vendors) == 1
    v = vendors[0]
    assert v["id"] == ws_id
    assert v["vendor_slug"] == "jyni"
    assert v["customer_facing_agent_name"] == "vega"
    assert v["widget_public_id"].startswith("wid_")
    # Does NOT include internal fields like plan_tier, budget, secrets.
    assert "plan_tier" not in v
    assert "budget_usd_monthly" not in v
    assert "stripe_customer_id" not in v


def test_me_end_user_omits_unlinked_vendors(client):
    """A vendor the end user is not linked to does NOT appear,
    even if a real workspace exists for that vendor_slug."""
    _make_vendor(slug="halo")  # not linked
    ws_jyni = _make_vendor(slug="jyni", public_id="wid_jyni_2")
    euid, token = _make_end_user()
    _link(euid, ws_jyni)

    r = client.get("/me/end-user", headers=_eu_auth(token))
    slugs = [v["vendor_slug"] for v in r.json()["linked_vendors"]]
    assert slugs == ["jyni"]


# ---------- GET /me/end-user/vendors/{slug} ---------- #


def test_get_vendor_by_slug_when_linked(client):
    ws_id = _make_vendor(slug="halo")
    euid, token = _make_end_user()
    _link(euid, ws_id)

    r = client.get(
        "/me/end-user/vendors/halo", headers=_eu_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == ws_id
    assert body["vendor_slug"] == "halo"
    # Phase 27.5: per-link settings included so the settings page
    # can render the form pre-populated in one fetch. Defaults:
    # notification_pref='all', display_name_override=None.
    assert body["notification_pref"] == "all"
    assert body["display_name_override"] is None


def test_get_vendor_by_slug_returns_custom_link_settings(client):
    """Phase 27.5: link settings reflect the end user's actual
    notification_pref + display_name_override, not defaults."""
    ws_id = _make_vendor(slug="custom")
    euid, token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=euid, workspace_id=ws_id,
            notification_pref="off",
            display_name_override="Alice S.",
        ))

    r = client.get(
        "/me/end-user/vendors/custom", headers=_eu_auth(token),
    )
    body = r.json()
    assert body["notification_pref"] == "off"
    assert body["display_name_override"] == "Alice S."


def test_get_vendor_by_slug_404_when_unlinked(client):
    """Vendor exists but end user is not linked → 404 (no leaking
    whether the slug exists)."""
    _make_vendor(slug="unlinked")
    _, token = _make_end_user()

    r = client.get(
        "/me/end-user/vendors/unlinked", headers=_eu_auth(token),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "vendor_not_found"


def test_get_vendor_by_slug_404_when_no_such_slug(client):
    """Same 404 shape when the slug simply doesn't exist."""
    _, token = _make_end_user()
    r = client.get(
        "/me/end-user/vendors/does-not-exist", headers=_eu_auth(token),
    )
    assert r.status_code == 404


def test_get_vendor_by_slug_requires_auth(client):
    r = client.get("/me/end-user/vendors/jyni")
    assert r.status_code == 401


# ---------- GET /me/end-user/vendors/{slug}/conversations ---------- #


def test_vendor_conversations_returns_only_end_users_own(client):
    """Alice has 2 conversations on JYNI; Bob has 1 on the same
    JYNI. Alice's call returns only her 2."""
    ws_id = _make_vendor(slug="jyni")
    alice_id, alice_token = _make_end_user(email="alice@example.com")
    bob_id, _ = _make_end_user(email="bob@example.com")
    _link(alice_id, ws_id)
    _link(bob_id, ws_id)

    a1 = _start_conv(ws_id, alice_id)
    a2 = _start_conv(ws_id, alice_id)
    _start_conv(ws_id, bob_id)  # Bob's conv

    r = client.get(
        "/me/end-user/vendors/jyni/conversations",
        headers=_eu_auth(alice_token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["vendor"]["vendor_slug"] == "jyni"
    conv_ids = {c["id"] for c in body["conversations"]}
    assert conv_ids == {a1, a2}


def test_vendor_conversations_scopes_to_workspace(client):
    """Alice linked to JYNI + Halo with conversations on both.
    Calling /me/end-user/vendors/jyni/conversations returns only
    her JYNI threads — Halo is scoped out by workspace_id."""
    ws_jyni = _make_vendor(slug="jyni", public_id="wid_j2")
    ws_halo = _make_vendor(slug="halo", public_id="wid_h2", agent_name="atlas")
    alice_id, token = _make_end_user()
    _link(alice_id, ws_jyni)
    _link(alice_id, ws_halo)

    jyni_conv = _start_conv(ws_jyni, alice_id)
    halo_conv = _start_conv(ws_halo, alice_id, agent="atlas")

    r = client.get(
        "/me/end-user/vendors/jyni/conversations",
        headers=_eu_auth(token),
    )
    conv_ids = [c["id"] for c in r.json()["conversations"]]
    assert jyni_conv in conv_ids
    assert halo_conv not in conv_ids


def test_vendor_conversations_orders_by_last_message_desc(client):
    """Most-recently-active first. The dashboard renders the list
    top-down so this is the order the end user sees."""
    ws_id = _make_vendor(slug="jyni")
    alice_id, token = _make_end_user()
    _link(alice_id, ws_id)

    # Create conv1, then conv2 with a later last_message_at.
    c1 = _start_conv(ws_id, alice_id)
    c2 = _start_conv(ws_id, alice_id)
    with session_scope() as s:
        s.get(WidgetConversation, c2).last_message_at = _now() + timedelta(seconds=10)

    r = client.get(
        "/me/end-user/vendors/jyni/conversations",
        headers=_eu_auth(token),
    )
    ordered = [c["id"] for c in r.json()["conversations"]]
    assert ordered == [c2, c1]


def test_vendor_conversations_404_when_unlinked(client):
    """Same shape as the vendor-resolve 404 path."""
    _make_vendor(slug="ghost")
    _, token = _make_end_user()
    r = client.get(
        "/me/end-user/vendors/ghost/conversations",
        headers=_eu_auth(token),
    )
    assert r.status_code == 404


def test_vendor_conversations_excludes_anonymous_threads(client):
    """If the vendor has an anonymous (anon_user_id, no end_user_id)
    conversation, it does NOT show up in the identified end user's
    list — only conversations with their end_user_id stamped."""
    ws_id = _make_vendor(slug="jyni")
    alice_id, token = _make_end_user()
    _link(alice_id, ws_id)

    # Anonymous conv (predates Alice signing in, or from a different
    # browser without the bearer).
    with session_scope() as s:
        s.add(WidgetConversation(
            id=str(uuid.uuid4()),
            workspace_id=ws_id,
            customer_facing_agent_name="vega",
            status="open",
            anon_user_id="anon-x",
            end_user_id=None,
            started_at=_now(),
            last_message_at=_now(),
        ))

    alice_conv = _start_conv(ws_id, alice_id)

    r = client.get(
        "/me/end-user/vendors/jyni/conversations",
        headers=_eu_auth(token),
    )
    conv_ids = [c["id"] for c in r.json()["conversations"]]
    assert conv_ids == [alice_conv]


def test_vendor_conversations_requires_auth(client):
    r = client.get("/me/end-user/vendors/jyni/conversations")
    assert r.status_code == 401
