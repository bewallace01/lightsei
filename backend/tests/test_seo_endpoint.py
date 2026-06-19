"""GET /workspaces/me/seo/drafts — surfaces Spica's drafted pages for the
dashboard to preview + publish."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from db import session_scope
from models import Event
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_draft(
    ws: str,
    *,
    keyword: str,
    slug: str,
    h1: str,
    body_html: str = "<h2>Why us</h2><p>We are great.</p>",
) -> None:
    with session_scope() as s:
        s.add(Event(
            workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="seo",
            kind="seo.page_drafted", timestamp=_now(),
            payload={
                "command_id": str(uuid.uuid4()),
                "keyword": keyword,
                "page": {
                    "title": f"{h1} — Top Choice",
                    "meta_description": "A great page.",
                    "slug": slug, "h1": h1,
                    "body_html": body_html,
                },
            },
        ))


def test_seo_drafts_returns_pages(client, alice):
    ws = alice["workspace"]["id"]
    _add_draft(ws, keyword="emergency plumber austin", slug="emergency-plumber-austin",
               h1="Emergency Plumber in Austin")
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/drafts", headers=h)
    assert r.status_code == 200, r.text
    drafts = r.json()["drafts"]
    assert len(drafts) == 1
    d = drafts[0]
    assert d["keyword"] == "emergency plumber austin"
    assert d["page"]["slug"] == "emergency-plumber-austin"
    assert d["page"]["h1"] == "Emergency Plumber in Austin"
    assert "<h2>" in d["page"]["body_html"]


def test_seo_drafts_empty_workspace(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/drafts", headers=h)
    assert r.status_code == 200
    assert r.json()["drafts"] == []


def test_seo_drafts_isolated_per_workspace(client, alice, bob):
    _add_draft(alice["workspace"]["id"], keyword="k", slug="s", h1="H")
    r = client.get("/workspaces/me/seo/drafts",
                   headers=auth_headers(bob["session_token"]))
    assert r.status_code == 200
    assert r.json()["drafts"] == []


def test_seo_drafts_sanitizes_untrusted_body_html(client, alice):
    ws = alice["workspace"]["id"]
    _add_draft(
        ws,
        keyword="k",
        slug="s",
        h1="H",
        body_html=(
            '<h2 onclick="steal()">Safe heading</h2>'
            '<script>window.evil = true</script>'
            '<p><a href="javascript:alert(1)" onmouseover="steal()">bad</a>'
            '<a href="/safe?x=1&y=2">safe</a></p>'
            '<img src=x onerror="steal()">'
        ),
    )
    r = client.get("/workspaces/me/seo/drafts",
                   headers=auth_headers(alice["session_token"]))
    assert r.status_code == 200, r.text
    html = r.json()["drafts"][0]["page"]["body_html"]
    assert "<h2>Safe heading</h2>" in html
    assert '<a>bad</a>' in html
    assert '<a href="/safe?x=1&amp;y=2" rel="noopener noreferrer">safe</a>' in html
    assert "script" not in html
    assert "onclick" not in html
    assert "onmouseover" not in html
    assert "onerror" not in html
    assert "javascript:" not in html
    assert "<img" not in html


# ---------- generate trigger (POST /workspaces/me/seo/generate) ---------- #

import seo_generate  # noqa: E402
from sqlalchemy import text as _text  # noqa: E402


def _seo_cmd_count(ws: str) -> int:
    with session_scope() as s:
        return s.execute(
            _text("SELECT count(*) FROM commands WHERE workspace_id=:w "
                  "AND agent_name='seo' AND kind='seo.generate_page'"),
            {"w": ws},
        ).scalar_one()


def test_seo_generate_enqueues_command(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/generate", headers=h,
                    json={"keyword": "emergency plumber austin", "page_type": "service"})
    assert r.status_code == 200, r.text
    assert r.json()["command_id"]
    assert _seo_cmd_count(ws) == 1
    with session_scope() as s:
        row = s.execute(
            _text("SELECT payload FROM commands WHERE workspace_id=:w "
                  "AND kind='seo.generate_page'"), {"w": ws},
        ).mappings().first()
        assert row["payload"]["keyword"] == "emergency plumber austin"
        assert row["payload"]["page_type"] == "service"


def test_seo_generate_empty_keyword_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/generate", headers=h, json={"keyword": "  "})
    assert r.status_code == 400


def test_seo_generate_unknown_page_type_defaults_landing(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    client.post("/workspaces/me/seo/generate", headers=h,
                json={"keyword": "k", "page_type": "not_a_type"})
    with session_scope() as s:
        row = s.execute(
            _text("SELECT payload FROM commands WHERE workspace_id=:w "
                  "AND kind='seo.generate_page'"), {"w": ws},
        ).mappings().first()
        assert row["payload"]["page_type"] == "landing"


def test_seo_deployed_reflects_agent(client, alice):
    ws = alice["workspace"]["id"]
    with session_scope() as s:
        assert seo_generate.seo_deployed(s, ws) is False
        s.execute(_text(
            "INSERT INTO agents (workspace_id, name, created_at, updated_at) "
            "VALUES (:w, 'seo', now(), now())"), {"w": ws})
    with session_scope() as s:
        assert seo_generate.seo_deployed(s, ws) is True
