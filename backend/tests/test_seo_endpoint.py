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


def _add_draft(ws: str, *, keyword: str, slug: str, h1: str) -> None:
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
                    "body_html": "<h2>Why us</h2><p>We are great.</p>",
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
