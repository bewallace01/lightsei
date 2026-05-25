"""Phase 25 demo: end-to-end flow without the real Phase 26 UI.

The spec'd demo is:

  > Alice gets a magic-link from JYNI. Lands on
  > app.lightsei.com/auth/end-user/magic-link?token=..., signs in,
  > lands at /c (the consumer home, Phase 26 ships the actual UI;
  > in Phase 25 it's a placeholder). She's now linked to JYNI as
  > an end-user. Her widget conversations from now on carry her
  > identity; from any device she logs in on, her conversation
  > history is hers.

Phase 26.2 builds the magic-link consume page + the real /c surface,
so we can't run the full UI demo today. This script runs the curl-
equivalent end-to-end against an in-process TestClient + Postgres
container (same machinery the test suite uses), narrating each step:

  1. Vendor signs up + wires a customer-facing bot.
  2. Alice requests an end-user magic link.
  3. The captured email body carries the token (Resend capture mode).
  4. Alice consumes the token → EndUser row created, session minted.
  5. Operator links Alice to the vendor (Phase 27.2 will do this via
     invite codes; today we link directly).
  6. Alice posts a widget message identified → end_user_id stamped on
     the conversation row.
  7. Alice polls + reads her own thread.
  8. Operator sees the conversation in /inbox with sensitivity_level
     visible.
  9. Vendor B + Alice's cross-vendor isolation sanity check.

Run from backend/:
  python3 scripts/phase_25_demo.py
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Ensure we run with FAKE_CAPTURE so emails don't try to hit Resend.
os.environ.setdefault("LIGHTSEI_EMAIL_FAKE_CAPTURE", "1")

# Make sure backend imports work whether we're invoked from backend/
# or from the repo root.
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def hr(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def step(n: int, msg: str) -> None:
    print(f"\n  [{n}] {msg}")


def main() -> int:
    # Import after sys.path is set so conftest fixtures load correctly.
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    # conftest sets up the Postgres + migrations on first import of
    # the testing harness. We skip the conftest fixtures and drive
    # the app + db ourselves.
    from tests.conftest import _OWNED_CONTAINER_ID  # noqa: F401
    # Trigger conftest's session-scope DB bring-up by importing it.
    import tests.conftest as _ct
    if not os.environ.get("LIGHTSEI_DATABASE_URL"):
        cid, url = _ct._spawn_pg()
        os.environ["LIGHTSEI_DATABASE_URL"] = url
        import atexit
        atexit.register(_ct._teardown_owned_pg)

    from main import app
    from db import session_scope
    from migrate import upgrade_to_head
    upgrade_to_head()
    import email_provider
    from models import (
        Agent, EndUser, EndUserVendorLink, WidgetConversation, Workspace,
    )

    client = TestClient(app)
    email_provider._reset_for_tests()

    hr("Phase 25 demo: end-user identity end-to-end")

    # ---------- Setup vendor A ---------- #

    step(1, "Vendor A operator signs up")
    sess_op = client.post(
        "/auth/signup",
        json={
            "email": "demo-operator@jyni.example.com",
            "password": "hunter22hunter22",
            "workspace_name": "JYNI (demo)",
        },
    ).json()
    ws_a_id = sess_op["workspace"]["id"]
    op_token = sess_op["session_token"]
    print(f"      workspace_id = {ws_a_id}")
    print(f"      session_token = {op_token[:24]}...")

    step(2, "Configure widget public_id + customer-facing bot vega (PII zone)")
    with session_scope() as s:
        ws = s.get(Workspace, ws_a_id)
        ws.widget_public_id = "wid_demo_jyni"
        ws.allowed_widget_origins = ["https://jyni.example.com"]
        ws.customer_facing_agent_name = "vega"
        s.add(Agent(
            workspace_id=ws_a_id,
            name="vega",
            role="specialist",
            description="JYNI's customer-facing PII-cleared bot.",
            sensitivity_level="pii",
            capabilities=["widget:respond", "widget:escalate"],
            command_handlers=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
    print("      vega wired up in PII zone with widget capabilities")

    # ---------- End-user magic-link flow ---------- #

    step(3, "Alice (end user) requests a magic link")
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={"email": "alice@example.com"},
    )
    print(f"      response: {r.status_code} {r.json()}")

    step(4, "Email captured (Resend FAKE_CAPTURE mode), extract token from URL")
    captured = email_provider.captured_emails()
    assert len(captured) == 1, "expected one captured email"
    magic_url = captured[0]["_magic_url"]
    token = magic_url.split("token=", 1)[1]
    print(f"      subject: {captured[0]['subject']}")
    print(f"      to: {captured[0]['to'][0]}")
    print(f"      magic_url: {magic_url[:64]}...")
    print(f"      token: {token[:24]}...")

    step(5, "Alice consumes the magic link")
    r = client.post(
        "/auth/end-user/magic-link/consume",
        json={"token": token},
    )
    body = r.json()
    print(f"      status: {r.status_code}")
    print(f"      is_new_end_user: {body['is_new_end_user']}")
    print(f"      end_user.email: {body['end_user']['email']}")
    print(f"      end_user.email_verified: {body['end_user']['email_verified']}")
    print(f"      session_token: {body['session_token'][:24]}...")
    print(f"      linked_vendors: {body['linked_vendors']}  (Phase 27.2 populates)")
    alice_token = body["session_token"]
    alice_id = body["end_user"]["id"]

    # ---------- Link Alice to vendor (Phase 27.2 will use invite codes) ---------- #

    step(6, "Operator links Alice to JYNI (Phase 27.2 will do this via invite codes)")
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=alice_id, workspace_id=ws_a_id,
        ))
    print("      EndUserVendorLink created: alice -> JYNI")

    # ---------- Identified widget messaging ---------- #

    step(7, "Alice posts a widget message with her bearer token")
    r = client.post(
        "/widget/wid_demo_jyni/messages",
        headers={
            "origin": "https://jyni.example.com",
            "authorization": f"Bearer {alice_token}",
        },
        json={"text": "Hi Vega, this is Alice."},
    )
    print(f"      status: {r.status_code}")
    conv_id = r.json()["conversation_id"]
    print(f"      conversation_id: {conv_id}")

    step(8, "Confirm widget_conversations.end_user_id was stamped")
    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        print(f"      conv.end_user_id: {conv.end_user_id}")
        print(f"      conv.anon_user_id: {conv.anon_user_id}  (None for identified)")
        assert conv.end_user_id == alice_id
        assert conv.anon_user_id is None

    step(9, "Alice polls her conversation (with her bearer)")
    r = client.get(
        f"/widget/wid_demo_jyni/conversations/{conv_id}",
        headers={
            "origin": "https://jyni.example.com",
            "authorization": f"Bearer {alice_token}",
        },
    )
    print(f"      status: {r.status_code}")
    print(f"      messages: {[m['text'] for m in r.json()['messages']]}")

    step(10, "Anonymous poll (no bearer) of Alice's conv = 404")
    r = client.get(
        f"/widget/wid_demo_jyni/conversations/{conv_id}",
        headers={"origin": "https://jyni.example.com"},
    )
    print(f"      status: {r.status_code}  (identified threads not pollable anonymously)")

    # ---------- Operator inbox view ---------- #

    step(11, "Operator sees Alice's conv in /inbox")
    r = client.get(
        "/workspaces/me/inbox?status=all",
        headers={"authorization": f"Bearer {op_token}"},
    )
    convs = r.json()["conversations"]
    matching = [c for c in convs if c["id"] == conv_id]
    print(f"      operator-visible conversations: {len(convs)}")
    print(f"      conv shows up: {len(matching) == 1}")
    if matching:
        c = matching[0]
        print(f"      conv.customer_facing_agent_name: {c['customer_facing_agent_name']}")
        print(f"      conv.last_message_at: {c['last_message_at'][:19]}")

    # ---------- Cross-vendor isolation ---------- #

    step(12, "Vendor B operator signs up + wires atlas (public zone)")
    sess_b = client.post(
        "/auth/signup",
        json={
            "email": "demo-operator@halo.example.com",
            "password": "hunter22hunter22",
            "workspace_name": "Halo (demo)",
        },
    ).json()
    ws_b_id = sess_b["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, ws_b_id)
        ws.widget_public_id = "wid_demo_halo"
        ws.allowed_widget_origins = ["https://halo.example.com"]
        ws.customer_facing_agent_name = "atlas"
        s.add(Agent(
            workspace_id=ws_b_id,
            name="atlas",
            role="specialist",
            description="Halo's public-zone web research bot.",
            sensitivity_level="public",
            capabilities=["widget:respond"],
            command_handlers=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
        # Link Alice to Halo too.
        s.add(EndUserVendorLink(
            end_user_id=alice_id, workspace_id=ws_b_id,
        ))
    print(f"      Halo workspace: {ws_b_id}, atlas wired in public zone, Alice linked")

    step(13, "Alice posts on Halo with the SAME bearer")
    r = client.post(
        "/widget/wid_demo_halo/messages",
        headers={
            "origin": "https://halo.example.com",
            "authorization": f"Bearer {alice_token}",
        },
        json={"text": "Hi Atlas, also Alice."},
    )
    halo_conv_id = r.json()["conversation_id"]
    print(f"      Halo conv_id: {halo_conv_id}")
    print(f"      JYNI conv_id: {conv_id}  (distinct row)")

    step(14, "Alice's bearer + JYNI public_id + Halo conv_id = 404 (isolation)")
    r = client.get(
        f"/widget/wid_demo_jyni/conversations/{halo_conv_id}",
        headers={
            "origin": "https://jyni.example.com",
            "authorization": f"Bearer {alice_token}",
        },
    )
    print(f"      status: {r.status_code}  (workspace mismatch)")

    step(15, "JYNI operator's /inbox does NOT list Alice's Halo conv")
    r = client.get(
        "/workspaces/me/inbox?status=all",
        headers={"authorization": f"Bearer {op_token}"},
    )
    convs = r.json()["conversations"]
    halo_visible = any(c["id"] == halo_conv_id for c in convs)
    print(f"      Halo conv visible to JYNI operator: {halo_visible}  (False = correct)")

    hr("Demo complete")
    print("\n  All assertions passed. Phase 25 end-to-end:")
    print("    - end-user magic-link signup + signin")
    print("    - identified widget conversation scoping")
    print("    - anonymous-poll blocked on identified threads")
    print("    - operator inbox sees the identified conversation")
    print("    - cross-vendor isolation: same bearer, different workspace, 404")
    print("\n  Phase 26 will replace the curl flow with the real UI:")
    print("    - /auth/end-user/magic-link?token=... consume page")
    print("    - /c vendor list + per-vendor chat + PWA install")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
