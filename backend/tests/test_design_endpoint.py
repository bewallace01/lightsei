"""Design format endpoints: enqueue a design.format command (Capella) and
poll its result by command id."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from db import session_scope
from models import Event
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_design_format_enqueues(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h,
                    json={"content": "<h1>Hi</h1>", "content_type": "page",
                          "accent_color": "#0a7"})
    assert r.status_code == 200, r.text
    cid = r.json()["command_id"]
    with session_scope() as s:
        row = s.execute(text("SELECT agent_name, kind, payload FROM commands "
                             "WHERE id = :id"), {"id": cid}).mappings().first()
        assert row["agent_name"] == "design"
        assert row["kind"] == "design.format"
        assert row["payload"]["content_type"] == "page"
        assert row["payload"]["accent_color"] == "#0a7"


def test_design_format_empty_content_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h, json={"content": "  "})
    assert r.status_code == 400


def test_design_format_unknown_type_defaults_generic(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x", "content_type": "weird"}).json()["command_id"]
    with session_scope() as s:
        row = s.execute(text("SELECT payload FROM commands WHERE id=:id"),
                        {"id": cid}).mappings().first()
        assert row["payload"]["content_type"] == "generic"


def test_design_result_pending_then_formatted(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x", "content_type": "page"}).json()["command_id"]
    # Pending before Capella emits.
    r = client.get(f"/workspaces/me/design/format/{cid}", headers=h)
    assert r.json()["status"] == "pending"
    # Simulate Capella's result event.
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="design",
                    kind="design.formatted", timestamp=_now(),
                    payload={"command_id": cid, "content_type": "page",
                             "output": "<!doctype html><body>styled</body>"}))
    r2 = client.get(f"/workspaces/me/design/format/{cid}", headers=h)
    body = r2.json()
    assert body["status"] == "formatted"
    assert "styled" in body["output"]
    assert body["content_type"] == "page"


def test_design_result_failed(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x"}).json()["command_id"]
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="design",
                    kind="design.crash", timestamp=_now(),
                    payload={"command_id": cid, "error": "boom"}))
    r = client.get(f"/workspaces/me/design/format/{cid}", headers=h)
    assert r.json()["status"] == "failed"
    assert r.json()["error"] == "boom"


def test_design_result_isolated_per_workspace(client, alice, bob):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    cid = client.post("/workspaces/me/design/format", headers=h,
                      json={"content": "x"}).json()["command_id"]
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="design",
                    kind="design.formatted", timestamp=_now(),
                    payload={"command_id": cid, "output": "x", "content_type": "page"}))
    # bob can't see alice's result.
    r = client.get(f"/workspaces/me/design/format/{cid}",
                   headers=auth_headers(bob["session_token"]))
    assert r.json()["status"] == "pending"


# ---------- match-my-site style extraction ---------- #
import design as _design_mod  # noqa: E402

_SITE_HTML = """
<html><head><style>
  body { font-family: 'Poppins', sans-serif; color: #2b2d42; }
  .btn { background: #ef233c; border-radius: 6px; }
  a { color: #ef233c; }
</style></head>
<body class="container"><div class="row"><a class="btn-primary">x</a></div></body></html>
"""


def test_extract_style_profile_pulls_fonts_colors_framework():
    p = _design_mod.extract_style_profile(_SITE_HTML)
    assert p is not None
    assert "Poppins" in p
    assert "#ef233c" in p          # brand color
    assert "Bootstrap" in p        # container/row/btn-primary
    assert "CSS to mirror" in p


def test_extract_style_profile_none_when_empty():
    assert _design_mod.extract_style_profile("<p>hi</p>") is None


def test_primary_color_picks_frequent_nonbw():
    assert _design_mod.primary_color(_SITE_HTML) == "#ef233c"


# ---- theme-aware extraction (regression for the "light site -> dark page" bug) ---- #

_LIGHT_CSS_SITE = """
<html><head><style>
  body { background: #f5f3ef; color: #1a2238; font-family: 'Inter', sans-serif; }
  h1, h2 { color: #1a2238; font-family: 'Playfair Display', serif; }
  a { color: #b5713a; }
</style></head><body><h1>Hi</h1><a href="#">link</a></body></html>
"""

# A light page built with Tailwind utilities. Its compiled CSS bundle contains
# dark palette hexes (slate-900 etc.) — exactly what made the old frequency
# counter emit dark "brand colors" and produce a dark page.
_LIGHT_TAILWIND_SITE = """
<html><head><style>
.bg-slate-900{background-color:#0f172a}.bg-indigo-950{background-color:#1e1b4b}
.text-amber-100{color:#fef3c7}.bg-stone-50{background-color:#fafaf9}
</style></head>
<body class="bg-stone-50 text-slate-900 flex">
<header class="bg-white"><nav class="flex"></nav></header>
<main class="bg-stone-50"><h1 class="text-slate-900">Working Capital</h1>
<p class="text-slate-700">para</p><a class="text-amber-700">cash advance</a></main>
</body></html>
"""

_DARK_SITE = """
<html><head><style>
  body { background: #0f172a; color: #e2e8f0; }
  h1 { color: #fde68a; } a { color: #f59e0b; }
</style></head><body><h1>x</h1></body></html>
"""


def test_extract_style_profile_detects_light_theme():
    p = _design_mod.extract_style_profile(_LIGHT_CSS_SITE)
    assert p is not None
    assert "Theme: LIGHT" in p
    assert "#f5f3ef" in p          # the real background, not a frequent palette hex
    assert "#1a2238" in p          # body text
    assert "#b5713a" in p          # link / accent
    assert "DARK" not in p.replace("Do NOT use a dark background or a dark theme.", "")


def test_extract_style_profile_light_tailwind_not_flipped_dark():
    # The bug: a light utility-class page whose CSS bundle holds dark palette
    # hexes was reported as dark. It must read LIGHT and must NOT advertise the
    # dark palette hexes as the design.
    p = _design_mod.extract_style_profile(_LIGHT_TAILWIND_SITE)
    assert p is not None
    assert "Theme: LIGHT" in p
    assert "#0f172a" not in p      # dark bundle hex must not leak as the design
    assert "#1e1b4b" not in p


def test_extract_style_profile_detects_dark_theme():
    p = _design_mod.extract_style_profile(_DARK_SITE)
    assert p is not None
    assert "Theme: DARK" in p
    assert "#0f172a" in p


def test_primary_color_skips_extremes_on_utility_page():
    # No usable link/body color rule + only near-black/near-white palette hexes
    # -> return None (model picks a tasteful default) rather than a dark
    # near-black "accent" on a light page.
    assert _design_mod.primary_color(_LIGHT_TAILWIND_SITE) is None


def test_design_format_match_url_folds_style_into_instructions(client, alice, monkeypatch):
    import main
    monkeypatch.setattr(main, "_fetch_page_html", lambda url: _SITE_HTML)
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h, json={
        "content": "<h1>Hi</h1>", "content_type": "page",
        "match_url": "mysite.com"})
    assert r.status_code == 200, r.text
    assert r.json()["matched_site"] == "https://mysite.com"
    cid = r.json()["command_id"]
    with session_scope() as s:
        row = s.execute(text("SELECT payload FROM commands WHERE id=:id"),
                        {"id": cid}).mappings().first()
        # The fetched site's style guide is in the command instructions.
        assert "Poppins" in row["payload"]["instructions"]
        assert row["payload"]["accent_color"] == "#ef233c"


def test_design_format_match_url_unreachable_degrades(client, alice, monkeypatch):
    import main
    monkeypatch.setattr(main, "_fetch_page_html", lambda url: None)  # fetch fails
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h, json={
        "content": "<h1>Hi</h1>", "match_url": "down.com"})
    assert r.status_code == 200  # still works, just no match
    assert r.json()["matched_site"] is None


# ---------- shell preview (repo-matched page) ---------- #

_PREVIEW_SHELL = """<!doctype html><html><head><link rel="stylesheet" href="/s.css">
</head><body><header><nav>Home</nav></header>
<main class="page-main"><h1>Old Title</h1><p>old body</p></main>
<footer>Footer</footer><script src="/app.js"></script></body></html>"""


def test_design_preview_in_shell_swaps_content(client, alice, monkeypatch):
    import main
    monkeypatch.setattr(main, "_fetch_page_html", lambda url: _PREVIEW_SHELL)
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/preview-in-shell", headers=h, json={
        "site_url": "https://mysite.com/some-page",
        "page": {"h1": "New Page", "body_html": "<p>new body</p>"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_shell"] is True
    html = body["html"]
    assert "New Page" in html and "new body" in html       # new content in
    assert "Old Title" not in html                          # old content out
    assert "<nav>" in html and "<footer>" in html           # shell kept
    assert "<script" not in html.lower()                    # scripts stripped
    assert '<base href="https://mysite.com/">' in html      # base for assets


def test_design_preview_in_shell_unreachable_502(client, alice, monkeypatch):
    import main
    monkeypatch.setattr(main, "_fetch_page_html", lambda url: None)
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/preview-in-shell", headers=h, json={
        "site_url": "https://down.com/p", "page": {"h1": "x"}})
    assert r.status_code == 502


def test_design_preview_in_shell_bad_url_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/preview-in-shell", headers=h, json={
        "site_url": "   ", "page": {"h1": "x"}})
    assert r.status_code == 400
