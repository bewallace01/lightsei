"""Spica auto-opens the publish PR (Phase 37.10): the pure orchestration core.

These exercise seo_autopublish without a DB or network (stubbed GitHub request
seam), the same way test_github_publish.py covers the lower-level mechanics.
The DB-bound pieces (workspace opt-in setting, the post_event ingest hook) are
covered in test_seo_autopublish_endpoint.py against the Postgres harness.
"""
from __future__ import annotations

import pytest

import github_publish
import seo_autopublish


# ---------- the gate ---------- #


def test_should_autopublish_requires_optin_repo_and_kind():
    ok = dict(enabled=True, repo_id="r1", event_kind="seo.page_drafted")
    assert seo_autopublish.should_autopublish(**ok) is True
    assert seo_autopublish.should_autopublish(**{**ok, "enabled": False}) is False
    assert seo_autopublish.should_autopublish(**{**ok, "repo_id": None}) is False
    assert seo_autopublish.should_autopublish(**{**ok, "repo_id": ""}) is False
    assert seo_autopublish.should_autopublish(**{**ok, "event_kind": "seo.audit_complete"}) is False


# ---------- payload extraction ---------- #


def test_page_from_event_reads_nested_page():
    payload = {
        "command_id": "c1", "keyword": "emergency plumber",
        "page": {"title": "T", "meta_description": "m", "slug": "s",
                 "h1": "H", "body_html": "<p>x</p>"},
    }
    page = seo_autopublish.page_from_event(payload)
    assert page is not None and page["title"] == "T" and page["body_html"] == "<p>x</p>"


def test_page_from_event_falls_back_to_top_level():
    payload = {"title": "T", "h1": "H", "body_html": "<p>x</p>"}
    page = seo_autopublish.page_from_event(payload)
    assert page is not None and page["title"] == "T"


def test_page_from_event_none_when_unusable():
    assert seo_autopublish.page_from_event(None) is None
    assert seo_autopublish.page_from_event({}) is None
    # title but no body
    assert seo_autopublish.page_from_event({"page": {"title": "T", "body_html": ""}}) is None
    # body but no title
    assert seo_autopublish.page_from_event({"page": {"title": "", "body_html": "<p>x</p>"}}) is None


def test_title_for_prefers_title_then_h1():
    assert seo_autopublish.title_for({"title": "A", "h1": "B"}) == "A"
    assert seo_autopublish.title_for({"h1": "B"}) == "B"
    assert seo_autopublish.title_for({}) == "Untitled page"


# ---------- orchestration (stubbed GitHub) ---------- #


class _FakeGithub:
    """Records calls; returns canned responses keyed by URL suffix/substring."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, *, method, url, token, json=None):
        self.calls.append((method, url, json))
        for suffix, resp in self.responses.items():
            if url.endswith(suffix) or suffix in url:
                return resp
        return {"status": 404, "body": {"message": "not found"}}


def _ok_responses():
    return {
        "/git/ref/heads/main": {"status": 200, "body": {"object": {"sha": "base123"}}},
        "/git/refs": {"status": 201, "body": {}},
        "/contents/": {"status": 201, "body": {}},
        "/pulls": {"status": 201, "body": {"html_url": "https://github.com/o/r/pull/7", "number": 7}},
    }


_PAGE = {"title": "Emergency Plumber Austin", "meta_description": "m",
         "slug": "emergency-plumber-austin", "h1": "Emergency Plumber in Austin",
         "body_html": "<p>We fix it.</p>"}


def test_orchestrate_publish_page_mode_opens_pr():
    fake = _FakeGithub(_ok_responses())
    out = seo_autopublish.orchestrate_publish(
        request=fake, token="t", owner="o", repo="r", base_branch="main",
        title="Emergency Plumber Austin", page=_PAGE, fmt="html")
    assert out["pr_url"].endswith("/pull/7") and out["pr_number"] == 7
    # Branch derived deterministically from the title (dedup hook).
    assert out["branch"] == "lightsei-seo/emergency-plumber-austin"
    # html render lands under public/pages/ (not src/pages/) -> no route wiring.
    assert "routed_path" not in out
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[1].endswith("/contents/public/pages/emergency-plumber-austin.html")


def test_orchestrate_publish_direct_content_with_route_wiring():
    app_tsx = (
        "import { lazy } from 'react';\n"
        "const HomePage = lazy(() =>\n"
        "  import('./pages/HomePage').then((m) => ({ default: m.HomePage }))\n"
        ");\n"
        "function App(){return (<Routes>\n"
        '  <Route path="/" element={<HomePage />} />\n'
        '  <Route path="*" element={<NotFound />} />\n'
        "</Routes>);}\n"
    )
    resp = {"/contents/src/App.tsx": {"status": 200, "body": {"sha": "appsha9"}}}
    resp.update(_ok_responses())
    fake = _FakeGithub(resp)
    # Make fetch_file return the App.tsx source (it reads /contents/..App.tsx).
    resp["/contents/src/App.tsx"] = {
        "status": 200,
        "body": {"encoding": "base64",
                 "content": __import__("base64").b64encode(app_tsx.encode()).decode(),
                 "sha": "appsha9"},
    }
    out = seo_autopublish.orchestrate_publish(
        request=fake, token="t", owner="o", repo="r", base_branch="main",
        title="Foo", content="export function FooPage(){ return null }",
        path="src/pages/FooPage.tsx")
    assert out["pr_number"] == 7
    assert out["routed_path"] == "/foo"
    # App.tsx committed as a companion file on the branch.
    puts = [c[1] for c in fake.calls if c[0] == "PUT"]
    assert any(p.endswith("/contents/src/App.tsx") for p in puts)
    assert any(p.endswith("/contents/src/pages/FooPage.tsx") for p in puts)
    # PR body mentions the route registration.
    pr_call = next(c for c in fake.calls if c[1].endswith("/pulls"))
    assert "/foo" in pr_call[2]["body"]


def test_orchestrate_publish_rejects_unsafe_path():
    with pytest.raises(ValueError):
        seo_autopublish.orchestrate_publish(
            request=_FakeGithub(_ok_responses()), token="t", owner="o", repo="r",
            base_branch="main", title="X", content="x", path="../escape.md")


def test_orchestrate_publish_rejects_empty_content():
    with pytest.raises(ValueError):
        seo_autopublish.orchestrate_publish(
            request=_FakeGithub(_ok_responses()), token="t", owner="o", repo="r",
            base_branch="main", title="X", content="   ", path="content/x.md")


def test_orchestrate_publish_propagates_branch_collision():
    # A re-emitted draft -> same deterministic branch -> GitHub 422 on create.
    # The error surfaces so the caller (background task) can swallow it as a
    # benign duplicate rather than opening a second PR.
    resp = _ok_responses()
    resp["/git/refs"] = {"status": 422, "body": {"message": "Reference already exists"}}
    with pytest.raises(github_publish.GithubPublishError):
        seo_autopublish.orchestrate_publish(
            request=_FakeGithub(resp), token="t", owner="o", repo="r",
            base_branch="main", title="Foo", page=_PAGE, fmt="html")
