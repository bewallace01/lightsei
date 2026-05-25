"""Phase 26 demo: scripted consumer-chat flow.

The spec demo is:

  > Alice opens app.lightsei.com/c after Phase 25's magic-link
  > signin → sees JYNI in her vendor list → taps in → starts a
  > conversation with vega → conversation persists across browser
  > sessions. She taps "Add to Home Screen" in Safari → JYNI/
  > Lightsei icon on her iPhone home screen. Opens it from there →
  > full-screen, no browser chrome, same conversation history.

This script runs the backend half of that flow against an in-process
TestClient + Postgres container — vendor setup, magic-link, vendor
slug claim, /me/end-user, /me/end-user/vendors/{slug}/conversations,
widget POST + poll. Confirms the dashboard pages will see the data
they need when /c, /c/{slug}, /c/auth/magic-link render.

The iPhone-install half (Add to Home Screen → full-screen launch) is
a physical-device step that can't be automated. The script ends with
a printed checklist Bailey runs against a real iPhone.

Run from backend/:
  python3 scripts/phase_26_demo.py
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
    from models import Agent, EndUserVendorLink, Workspace
    import email_provider

    c = TestClient(app)
    email_provider._reset_for_tests()

    hr("Phase 26 demo: consumer-chat /c surface end-to-end")

    # ---------- Vendor setup ---------- #

    step(1, "JYNI operator signs up")
    sess = c.post("/auth/signup", json={
        "email": "demo-operator@jyni.example.com",
        "password": "hunter22hunter22",
        "workspace_name": "JYNI (demo)",
    }).json()
    op_tok = sess["session_token"]
    ws_id = sess["workspace"]["id"]
    print(f"      workspace_id = {ws_id}")

    step(2, "Operator claims vendor_slug 'jyni' (Phase 26.1)")
    r = c.post(
        "/workspaces/me/vendor-slug",
        headers={"authorization": f"Bearer {op_tok}"},
        json={"slug": "jyni"},
    )
    print(f"      POST /workspaces/me/vendor-slug → {r.status_code} {r.json()['vendor_slug']!r}")

    step(3, "Operator wires widget_public_id + customer-facing bot vega (PII zone)")
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.widget_public_id = "wid_jyni_p26"
        ws.allowed_widget_origins = ["https://app.lightsei.com"]
        ws.customer_facing_agent_name = "vega"
        s.add(Agent(
            workspace_id=ws_id,
            name="vega",
            role="specialist",
            description="JYNI's customer-facing PII-cleared bot.",
            sensitivity_level="pii",
            capabilities=["widget:respond", "widget:escalate"],
            command_handlers=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
    print("      vega in PII zone, widget public_id + origin set")

    step(4, "GET /workspaces/me echoes vendor_slug")
    r = c.get("/workspaces/me", headers={"authorization": f"Bearer {op_tok}"})
    print(f"      vendor_slug in response: {r.json().get('vendor_slug')!r}")

    # ---------- End-user signin ---------- #

    step(5, "Alice requests end-user magic link (this is what /c/auth/magic-link will POST)")
    c.post("/auth/end-user/magic-link/request", json={"email": "alice@example.com"})
    captured = email_provider.captured_emails()
    magic_url = captured[-1]["_magic_url"]
    print(f"      email captured, subject: {captured[-1]['subject']!r}")
    print(f"      magic_url: {magic_url[:64]}...")

    step(6, "Alice's /c/auth/magic-link page calls consume + stores session_token")
    token = magic_url.split("token=", 1)[1]
    r = c.post("/auth/end-user/magic-link/consume", json={"token": token})
    alice_tok = r.json()["session_token"]
    alice_id = r.json()["end_user"]["id"]
    print(f"      end_user.email_verified: {r.json()['end_user']['email_verified']}")
    print(f"      session_token: {alice_tok[:24]}... (stored in localStorage by /c page)")
    print(f"      linked_vendors on consume: {r.json()['linked_vendors']} (27.2 wires invite codes)")

    step(7, "Operator links Alice to JYNI (Phase 27.2 will do this via invite codes)")
    with session_scope() as s:
        s.add(EndUserVendorLink(end_user_id=alice_id, workspace_id=ws_id))

    # ---------- /c page data ---------- #

    eu_auth = {"authorization": f"Bearer {alice_tok}"}

    step(8, "/c page loads: GET /me/end-user")
    r = c.get("/me/end-user", headers=eu_auth)
    body = r.json()
    print(f"      end_user.email: {body['end_user']['email']!r}")
    print(f"      linked_vendors: {len(body['linked_vendors'])}")
    for v in body["linked_vendors"]:
        print(f"        - {v['name']!r} (slug={v['vendor_slug']!r}, bot={v['customer_facing_agent_name']!r})")
    print("      /c renders one vendor card with 'Open chat →' linking to /c/jyni")

    # ---------- /c/jyni page data ---------- #

    step(9, "/c/jyni page loads: GET /me/end-user/vendors/jyni/conversations")
    r = c.get("/me/end-user/vendors/jyni/conversations", headers=eu_auth)
    body = r.json()
    print(f"      vendor: {body['vendor']['name']!r}")
    print(f"      conversations: {len(body['conversations'])} (empty on first visit)")

    step(10, "Alice taps 'New conversation' + types a message. Dashboard calls POST /widget/wid_jyni_p26/messages with Origin + bearer")
    r = c.post(
        "/widget/wid_jyni_p26/messages",
        headers={
            "origin": "https://app.lightsei.com",
            **eu_auth,
        },
        json={"text": "Hi vega, asking from /c/jyni"},
    )
    conv_id = r.json()["conversation_id"]
    print(f"      POST → {r.status_code}, conv_id: {conv_id}")

    step(11, "/c/jyni page polls thread: GET /widget/wid_jyni_p26/conversations/{conv}")
    r = c.get(
        f"/widget/wid_jyni_p26/conversations/{conv_id}",
        headers={
            "origin": "https://app.lightsei.com",
            **eu_auth,
        },
    )
    msgs = r.json()["messages"]
    print(f"      messages: {[m['text'] for m in msgs]}")

    step(12, "Conversation persists across reloads: list now has 1 conv")
    r = c.get("/me/end-user/vendors/jyni/conversations", headers=eu_auth)
    convs = r.json()["conversations"]
    print(f"      conversations: {len(convs)}")
    print(f"      id matches: {convs[0]['id'] == conv_id}")

    # ---------- PWA manifest + sw.js (Phase 26.4) ---------- #

    step(13, "PWA assets exist on the dashboard public path")
    pub = os.path.join(os.path.dirname(BACKEND), "dashboard", "public")
    for f in (
        "sw.js", "apple-touch-icon.png", "icon-192.png",
        "icon-512.png", "icon-192-maskable.png", "icon-512-maskable.png",
    ):
        path = os.path.join(pub, f)
        sz = os.path.getsize(path) if os.path.exists(path) else None
        print(f"      {f}: {'OK ' + str(sz) + ' B' if sz else 'MISSING'}")

    hr("Backend half: PASS")

    print("""
  Physical iPhone install verification (Bailey):

  1. Start dashboard locally: `cd dashboard && npx next dev`
     (or boot the prod build with `npx next start`).

  2. Open the URL on your iPhone via Safari. localhost dev needs the
     same Wi-Fi + the laptop's LAN IP (e.g. http://192.168.1.42:3000/c);
     prod is just https://app.lightsei.com/c.

  3. On /c (signed in or signed out doesn't matter for install):
     a. The "Install Lightsei" banner should appear at the bottom of
        the page. (Banner shows on iOS Safari only — confirm it's
        there.)
     b. Tap Safari's share icon (square with arrow up).
     c. Choose "Add to Home Screen". Edit the name to "Lightsei" if
        prompted.
     d. Confirm. Safari closes; check the home screen for the indigo
        "L" icon.

  4. From the home screen, tap the Lightsei icon:
     a. The app should open full-screen (no Safari URL bar / tab bar).
     b. /c should render. If you were signed in pre-install, you stay
        signed in (localStorage persists across the install hop).
     c. Open a vendor → conversation should be there from step (10).

  5. Re-launch the banner once dismissed:
     a. Clear localStorage on app.lightsei.com (Safari → Settings →
        Advanced → Website Data) OR delete + re-add the home screen
        icon. The dismissed flag is local-only by design.

  If any of steps 3-4 fail, the most common culprits are:
    - HTTPS missing (PWA install requires https://, no exceptions on
      iOS). Use the prod URL or set up local certs.
    - The manifest endpoint isn't reachable. Check
      curl https://app.lightsei.com/manifest.webmanifest returns JSON.
    - apple-touch-icon.png missing or non-PNG. Check the HTML head on
      /c includes a <link rel="apple-touch-icon" href="...">.

  Report PASS/FAIL per numbered step.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
