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


def test_delete_seo_draft_removes_it(client, alice):
    ws = alice["workspace"]["id"]
    _add_draft(ws, keyword="k1", slug="s1", h1="Keep me")
    _add_draft(ws, keyword="k2", slug="s2", h1="Delete me")
    h = auth_headers(alice["session_token"])
    drafts = client.get("/workspaces/me/seo/drafts", headers=h).json()["drafts"]
    target = next(d for d in drafts if d["page"]["h1"] == "Delete me")

    r = client.delete(f"/workspaces/me/seo/drafts/{target['id']}", headers=h)
    assert r.status_code == 200, r.text

    remaining = client.get("/workspaces/me/seo/drafts", headers=h).json()["drafts"]
    assert [d["page"]["h1"] for d in remaining] == ["Keep me"]
    # Deleting again is a 404 (already gone).
    assert client.delete(
        f"/workspaces/me/seo/drafts/{target['id']}", headers=h).status_code == 404


def test_delete_seo_draft_cross_workspace_404(client, alice, bob):
    ws = alice["workspace"]["id"]
    _add_draft(ws, keyword="k", slug="s", h1="Alice's")
    aid = client.get("/workspaces/me/seo/drafts",
                     headers=auth_headers(alice["session_token"])).json()["drafts"][0]["id"]
    # Bob can't delete Alice's draft.
    r = client.delete(f"/workspaces/me/seo/drafts/{aid}",
                      headers=auth_headers(bob["session_token"]))
    assert r.status_code == 404
    # Alice's draft is untouched.
    assert len(client.get("/workspaces/me/seo/drafts",
                          headers=auth_headers(alice["session_token"])).json()["drafts"]) == 1


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


# ---------- audit view + on-demand audit ---------- #
import feeder as _feeder  # noqa: E402


def _add_audit(ws: str, *, url: str, score: int, issues: int, warnings: int) -> None:
    with session_scope() as s:
        s.add(Event(
            workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="seo",
            kind="seo.audit_complete", timestamp=_now(),
            payload={"url": url, "reachable": True, "score": score,
                     "issues": issues, "warnings": warnings,
                     "findings": [{"check": "title", "status": "warn",
                                   "detail": "short", "recommendation": "longer"}]},
        ))


def test_seo_audit_view_returns_latest(client, alice):
    ws = alice["workspace"]["id"]
    _add_audit(ws, url="https://x.com", score=85, issues=0, warnings=3)
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/audit", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["latest"]["score"] == 85
    assert body["latest"]["warnings"] == 3
    assert body["latest"]["findings"][0]["check"] == "title"


def test_seo_audit_view_empty(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/audit", headers=h)
    assert r.status_code == 200
    assert r.json()["latest"] is None


def test_seo_audit_view_exposes_configured_url(client, alice):
    ws = alice["workspace"]["id"]
    with session_scope() as s:
        _feeder.set_feeder_config(s, ws, _feeder.FEEDER_SEO_AUDIT,
                                  {"url": "https://mysite.com"}, _now())
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/audit", headers=h)
    assert r.json()["configured_url"] == "https://mysite.com"


def test_seo_run_audit_with_url(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/audit", headers=h, json={"url": "mysite.com"})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://mysite.com"
    with session_scope() as s:
        n = s.execute(_text("SELECT count(*) FROM commands WHERE workspace_id=:w "
                            "AND kind='seo.audit'"), {"w": ws}).scalar_one()
        assert n == 1


def test_seo_run_audit_falls_back_to_configured(client, alice):
    ws = alice["workspace"]["id"]
    with session_scope() as s:
        _feeder.set_feeder_config(s, ws, _feeder.FEEDER_SEO_AUDIT,
                                  {"url": "https://configured.com"}, _now())
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/audit", headers=h, json={})
    assert r.status_code == 200
    assert r.json()["url"] == "https://configured.com"


def test_seo_run_audit_no_url_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/audit", headers=h, json={})
    assert r.status_code == 400


# ---------- site crawl ---------- #


def _add_crawl(ws: str, *, pages: int, avg: int) -> None:
    with session_scope() as s:
        s.add(Event(
            workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="seo",
            kind="seo.crawl_complete", timestamp=_now(),
            payload={"start_url": "https://x.com/", "pages_audited": pages,
                     "average_score": avg, "lowest_score": avg - 5,
                     "pages": [{"url": "https://x.com/", "score": avg,
                                "issues": 0, "warnings": 1, "reachable": True}],
                     "top_findings": [{"check": "title", "pages": 2}]},
        ))


def test_seo_crawl_run_with_url(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/crawl", headers=h,
                    json={"url": "mysite.com", "max_pages": 4})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://mysite.com"
    with session_scope() as s:
        row = s.execute(_text("SELECT payload FROM commands WHERE workspace_id=:w "
                              "AND kind='seo.crawl'"), {"w": ws}).mappings().first()
        assert row["payload"]["max_pages"] == 4


def test_seo_crawl_no_url_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/crawl", headers=h, json={})
    assert r.status_code == 400


def test_seo_crawl_view_latest(client, alice):
    ws = alice["workspace"]["id"]
    _add_crawl(ws, pages=3, avg=82)
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/crawl", headers=h)
    assert r.status_code == 200, r.text
    latest = r.json()["latest"]
    assert latest["pages_audited"] == 3
    assert latest["average_score"] == 82
    assert latest["top_findings"][0]["check"] == "title"


def test_seo_crawl_view_empty(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/crawl", headers=h)
    assert r.json()["latest"] is None


# ---------- page-idea suggestions ---------- #


def test_seo_suggestions_run_enqueues(client, alice):
    ws = alice["workspace"]["id"]
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/seo/suggestions", headers=h,
                    json={"business_context": "Acme Plumbing", "count": 4})
    assert r.status_code == 200, r.text
    assert r.json()["command_id"]
    with session_scope() as s:
        row = s.execute(_text("SELECT payload FROM commands WHERE workspace_id=:w "
                              "AND kind='seo.suggest'"), {"w": ws}).mappings().first()
        assert row["payload"]["count"] == 4
        assert row["payload"]["business_context"] == "Acme Plumbing"


def test_seo_suggestions_pulls_crawl_pages(client, alice):
    ws = alice["workspace"]["id"]
    _add_crawl(ws, pages=2, avg=80)  # adds a crawl with one page url x.com/
    h = auth_headers(alice["session_token"])
    client.post("/workspaces/me/seo/suggestions", headers=h, json={})
    with session_scope() as s:
        row = s.execute(_text("SELECT payload FROM commands WHERE workspace_id=:w "
                              "AND kind='seo.suggest'"), {"w": ws}).mappings().first()
        assert "https://x.com/" in (row["payload"].get("existing_pages") or [])


def test_seo_suggestions_view_latest(client, alice):
    ws = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Event(workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="seo",
                    kind="seo.suggestions", timestamp=_now(),
                    payload={"suggestions": [
                        {"keyword": "emergency plumber austin", "page_type": "service",
                         "rationale": "high intent"}]}))
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/suggestions", headers=h)
    assert r.status_code == 200, r.text
    s = r.json()["suggestions"]
    assert len(s) == 1 and s[0]["keyword"] == "emergency plumber austin"


def test_seo_suggestions_view_empty(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/suggestions", headers=h)
    assert r.json()["suggestions"] == []
