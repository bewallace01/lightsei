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
