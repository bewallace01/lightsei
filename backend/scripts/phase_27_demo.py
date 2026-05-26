"""Phase 27 demo: cross-vendor invite + settings + soft-revoke flow.

End-to-end scripted run against an in-process TestClient. Covers
the full Phase 27 surface:

  1. Vendor setup (signup + slug claim + bot wired).
  2. Operator mints an invite code (Phase 27.2 + UI shipped in 27.3).
  3. End-user signup via magic link.
  4. End user redeems the code -> linked to the vendor.
  5. /me/end-user/vendors lists the vendor with the new link.
  6. End user posts a widget message identified.
  7. End user patches per-vendor settings (display name + notif).
  8. End user soft-revokes the link.
  9. End user can still GET the past conversation (read-only, 27.6).
 10. End user CANNOT POST a new message into that conversation
     (write-gate blocks soft-revoked).
 11. /me/end-user/vendors excludes the soft-revoked vendor.

Run from backend/:
  python3 scripts/phase_27_demo.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("LIGHTSEI_EMAIL_FAKE_CAPTURE", "1")

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
    import tests.conftest as _ct
    if not os.environ.get("LIGHTSEI_DATABASE_URL"):
        cid, url = _ct._spawn_pg()
        os.environ["LIGHTSEI_DATABASE_URL"] = url
        import atexit
        atexit.register(_ct._teardown_owned_pg)

    from fastapi.testclient import TestClient
    from main import app
    from migrate import upgrade_to_head
    upgrade_to_head()
    from db import session_scope
    from models import Agent, Workspace
    import email_provider

    c = TestClient(app)
    email_provider._reset_for_tests()

    hr("Phase 27 demo: invite codes + per-vendor settings + soft-revoke")

    # ---------- Vendor setup ---------- #

    step(1, "Operator signup + slug claim 'jyni' + vega bot wired")
    sess = c.post("/auth/signup", json={
        "email": "ops@jyni.example.com",
        "password": "hunter22hunter22",
        "workspace_name": "JYNI",
    }).json()
    op_tok = sess["session_token"]
    ws_id = sess["workspace"]["id"]
    c.post(
        "/workspaces/me/vendor-slug",
        headers={"authorization": f"Bearer {op_tok}"},
        json={"slug": "jyni"},
    )
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.widget_public_id = "wid_jyni_p27"
        ws.allowed_widget_origins = ["https://app.lightsei.com"]
        ws.customer_facing_agent_name = "vega"
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            description="JYNI's PII bot.", sensitivity_level="pii",
            capabilities=["widget:respond", "widget:escalate"],
            command_handlers=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
    print(f"      workspace_id={ws_id}, vendor_slug=jyni, bot=vega")

    # ---------- Mint invite ---------- #

    step(2, "Operator mints 1 invite code (Phase 27.2 POST /workspaces/me/end-user-invites)")
    r = c.post(
        "/workspaces/me/end-user-invites",
        headers={"authorization": f"Bearer {op_tok}"},
        json={"count": 1, "ttl_days": 7},
    )
    invite_code = r.json()["codes"][0]["code"]
    print(f"      minted: {invite_code}")

    # ---------- End-user signup ---------- #

    step(3, "End user signs up via magic link (Phase 25.2)")
    c.post("/auth/end-user/magic-link/request", json={"email": "alice@example.com"})
    magic_url = email_provider.captured_emails()[-1]["_magic_url"]
    token = magic_url.split("token=", 1)[1]
    r = c.post("/auth/end-user/magic-link/consume", json={"token": token})
    alice_tok = r.json()["session_token"]
    print(f"      alice signed in: is_new={r.json()['is_new_end_user']}")

    eu_auth = {"authorization": f"Bearer {alice_tok}"}

    # ---------- Redeem invite ---------- #

    step(4, "Alice redeems the invite code (POST /me/end-user/redeem-invite)")
    r = c.post(
        "/me/end-user/redeem-invite",
        headers=eu_auth, json={"code": invite_code},
    )
    print(f"      linked={r.json()['linked']}, vendor={r.json()['vendor']['name']!r}")

    # ---------- Vendor list ---------- #

    step(5, "Alice's vendor list shows JYNI (GET /me/end-user/vendors)")
    r = c.get("/me/end-user/vendors", headers=eu_auth)
    print(f"      vendors: {[(v['name'], v['unread_count']) for v in r.json()['vendors']]}")

    # ---------- Identified chat ---------- #

    step(6, "Alice posts a widget message identified")
    r = c.post(
        "/widget/wid_jyni_p27/messages",
        headers={"origin": "https://app.lightsei.com", **eu_auth},
        json={"text": "Hi vega, this is Alice via /c"},
    )
    conv_id = r.json()["conversation_id"]
    print(f"      conv_id={conv_id}, status={r.status_code}")

    # ---------- Patch settings ---------- #

    step(7, "Alice patches per-vendor settings (display_name + notif=off)")
    r = c.patch(
        f"/me/end-user/vendors/{ws_id}",
        headers=eu_auth,
        json={"display_name_override": "Alice S.", "notification_pref": "off"},
    )
    print(f"      {r.json()}")

    step(8, "GET /me/end-user/vendors/jyni reflects custom settings (Phase 27.5)")
    r = c.get("/me/end-user/vendors/jyni", headers=eu_auth)
    body = r.json()
    print(f"      display_name_override={body['display_name_override']!r}")
    print(f"      notification_pref={body['notification_pref']!r}")

    # ---------- Soft-revoke ---------- #

    step(9, "Alice unsubscribes (DELETE /me/end-user/vendors/{id})")
    r = c.delete(f"/me/end-user/vendors/{ws_id}", headers=eu_auth)
    print(f"      {r.json()}")

    # ---------- Read-only after soft-revoke (Phase 27.6) ---------- #

    step(10, "After soft-revoke, Alice can STILL GET her past conversation (read-only)")
    r = c.get(
        f"/widget/wid_jyni_p27/conversations/{conv_id}",
        headers={"origin": "https://app.lightsei.com", **eu_auth},
    )
    print(f"      GET status={r.status_code}, messages={[m['text'] for m in r.json()['messages']]}")
    assert r.status_code == 200, "Phase 27.6: read access must survive soft-revoke"

    step(11, "But Alice CANNOT POST a new message into the conv (write-gate blocks)")
    r = c.post(
        "/widget/wid_jyni_p27/messages",
        headers={"origin": "https://app.lightsei.com", **eu_auth},
        json={"text": "follow-up after unsubscribe", "conversation_id": conv_id},
    )
    print(f"      POST status={r.status_code} (expected 404)")
    assert r.status_code == 404, "Phase 27.6: write must fail after soft-revoke"

    step(12, "/me/end-user/vendors excludes the soft-revoked vendor")
    r = c.get("/me/end-user/vendors", headers=eu_auth)
    print(f"      vendors: {[v['name'] for v in r.json()['vendors']]} (expected [])")
    assert r.json()["vendors"] == []

    hr("Phase 27 demo: PASS")

    print("""
  Verified flow:
    operator mint -> end-user redeem -> identified chat ->
    per-vendor settings -> soft-revoke -> read-only past convs ->
    write blocked -> vendor list cleared.

  This closes Phase 27. Operator + end-user can now manage the full
  cross-vendor subscription lifecycle.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
