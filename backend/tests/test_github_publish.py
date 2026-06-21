"""Git-deploy publish path: commit a generated SEO page to a connected repo
and open a PR (covers Vercel / Cloudflare / Railway / Netlify via git).

Pure helper tests (stubbed GitHub request seam) + the endpoint
(/workspaces/me/github/publish-page) wiring.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

import github_publish
import secrets_crypto
from db import session_scope
from models import GitHubIntegration, GithubConnection, GithubRepo
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- pure helpers ---------- #


def test_branch_name_for_is_deterministic_and_clean():
    assert github_publish.branch_name_for("Emergency Plumber, Austin!") == \
        "lightsei-seo/emergency-plumber-austin"
    assert github_publish.branch_name_for("") == "lightsei-seo/page"


def test_is_safe_repo_path():
    assert github_publish.is_safe_repo_path("content/blog/x.md")
    assert not github_publish.is_safe_repo_path("/etc/passwd")
    assert not github_publish.is_safe_repo_path("../x.md")
    assert not github_publish.is_safe_repo_path("a/../../b.md")
    assert not github_publish.is_safe_repo_path("")


class _FakeGithub:
    """Records calls and returns canned responses keyed by URL suffix."""
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


def test_publish_page_happy_path():
    fake = _FakeGithub(_ok_responses())
    out = github_publish.publish_page_to_repo(
        request=fake, token="t", owner="o", repo="r", base_branch="main",
        path="content/x.md", content="# hi", branch_name="lightsei-seo/x",
        commit_message="Add page", pr_title="Add SEO page", pr_body="body")
    assert out["pr_url"].endswith("/pull/7")
    assert out["pr_number"] == 7 and out["branch"] == "lightsei-seo/x"
    # Steps in order: read base ref, create branch, look up the file's sha
    # (new file -> none), commit it, open PR.
    methods = [c[0] for c in fake.calls]
    assert methods == ["GET", "POST", "GET", "PUT", "POST"]
    # The file body was base64-encoded.
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert "content" in put[2] and put[2]["content"] != "# hi"


def test_publish_commits_extra_files_then_opens_pr():
    # extra_files (e.g. an App.tsx route registration) are committed on the same
    # branch before the PR. An existing file's sha is fetched so the update
    # isn't rejected.
    # The App.tsx contents call returns an existing blob sha; everything else is
    # new. The specific key must precede the generic "/contents/" so the fake's
    # substring match resolves App.tsx to the sha response.
    resp = {"/contents/src/App.tsx": {"status": 200, "body": {"sha": "appsha9"}}}
    resp.update(_ok_responses())
    fake = _FakeGithub(resp)
    out = github_publish.publish_page_to_repo(
        request=fake, token="t", owner="o", repo="r", base_branch="main",
        path="src/pages/FooPage.tsx", content="export function FooPage(){}",
        branch_name="lightsei-seo/foo", commit_message="Add page",
        pr_title="Add SEO page", pr_body="body",
        extra_files=[("src/App.tsx", "wired app source")])
    assert out["pr_number"] == 7
    puts = [c for c in fake.calls if c[0] == "PUT"]
    # Two files committed: the page and App.tsx.
    paths = [c[1] for c in puts]
    assert any(p.endswith("/contents/src/pages/FooPage.tsx") for p in paths)
    app_put = next(c for c in puts if c[1].endswith("/contents/src/App.tsx"))
    # The App.tsx update carried the looked-up sha (in-place update).
    assert app_put[2].get("sha") == "appsha9"


def test_publish_rejects_unsafe_extra_file_path():
    import pytest
    with pytest.raises(github_publish.GithubPublishError):
        github_publish.publish_page_to_repo(
            request=_FakeGithub(_ok_responses()), token="t", owner="o", repo="r",
            base_branch="main", path="src/pages/FooPage.tsx", content="x",
            branch_name="b", commit_message="m", pr_title="t", pr_body="b",
            extra_files=[("../../etc/passwd", "nope")])


def test_publish_rejects_unsafe_path():
    import pytest
    with pytest.raises(github_publish.GithubPublishError):
        github_publish.publish_page_to_repo(
            request=_FakeGithub(_ok_responses()), token="t", owner="o", repo="r",
            base_branch="main", path="../escape.md", content="x",
            branch_name="b", commit_message="m", pr_title="t", pr_body="b")


def test_publish_errors_when_base_branch_missing():
    import pytest
    fake = _FakeGithub({"/git/ref/heads/main": {"status": 404, "body": {}}})
    with pytest.raises(github_publish.GithubPublishError) as e:
        github_publish.publish_page_to_repo(
            request=fake, token="t", owner="o", repo="r", base_branch="main",
            path="x.md", content="x", branch_name="b", commit_message="m",
            pr_title="t", pr_body="b")
    assert "base branch" in str(e.value)


def test_publish_errors_when_branch_exists():
    import pytest
    resp = _ok_responses()
    resp["/git/refs"] = {"status": 422, "body": {"message": "exists"}}
    with pytest.raises(github_publish.GithubPublishError) as e:
        github_publish.publish_page_to_repo(
            request=_FakeGithub(resp), token="t", owner="o", repo="r",
            base_branch="main", path="x.md", content="x",
            branch_name="lightsei-seo/x", commit_message="m", pr_title="t", pr_body="b")
    assert "already exists" in str(e.value)


# ---------- endpoint ---------- #


def _connect_repo(workspace_id: str) -> str:
    """Add a GithubConnection + GithubRepo to a workspace. Returns repo_id."""
    cid = str(uuid.uuid4()); rid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(GithubConnection(id=cid, workspace_id=workspace_id,
                               encrypted_token=secrets_crypto.encrypt("ghu-token"),
                               auth_kind="oauth", created_at=_now(), updated_at=_now()))
        s.add(GithubRepo(id=rid, workspace_id=workspace_id, connection_id=cid,
                         repo_owner="acme", repo_name="site", branch="main",
                         encrypted_webhook_secret=secrets_crypto.encrypt("wh"),
                         is_active=True, created_at=_now(), updated_at=_now()))
    return rid


def test_publish_endpoint_opens_pr(client, alice, monkeypatch):
    rid = _connect_repo(alice["workspace"]["id"])
    monkeypatch.setattr(
        github_publish, "publish_page_to_repo",
        lambda **k: {"pr_url": "https://github.com/acme/site/pull/3",
                     "pr_number": 3, "branch": k["branch_name"]})
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": rid, "path": "content/blog/plumber-austin.md",
        "content": "# Plumber in Austin\nWe help.", "title": "Plumber in Austin"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr_number"] == 3
    # branch derives from the title "Plumber in Austin".
    assert body["branch"] == "lightsei-seo/plumber-in-austin"


def test_publish_endpoint_structured_page_markdown(client, alice, monkeypatch):
    """The structured page + format mode renders the file server-side and
    commits it at the format's default path."""
    rid = _connect_repo(alice["workspace"]["id"])
    captured = {}

    def _fake(**k):
        captured.update(k)
        return {"pr_url": "https://github.com/acme/site/pull/9", "pr_number": 9,
                "branch": k["branch_name"]}

    monkeypatch.setattr(github_publish, "publish_page_to_repo", _fake)
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": rid, "title": "Inventory Tips", "format": "markdown",
        "page": {"title": "Inventory Tips", "meta_description": "Cut costs.",
                 "slug": "inventory-tips", "h1": "Inventory Tips",
                 "body_html": "<p>hi</p>"}})
    assert r.status_code == 200, r.text
    # Rendered to a markdown file at the markdown default path, with front matter.
    assert captured["path"] == "content/inventory-tips.md"
    assert captured["content"].startswith("---\n")
    assert 'title: "Inventory Tips"' in captured["content"]


def test_publish_endpoint_unknown_format_400(client, alice):
    rid = _connect_repo(alice["workspace"]["id"])
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": rid, "title": "x", "format": "pdf",
        "page": {"title": "x", "meta_description": "d", "slug": "s",
                 "h1": "H", "body_html": "b"}})
    assert r.status_code == 400


def test_publish_endpoint_unsafe_path_400(client, alice):
    rid = _connect_repo(alice["workspace"]["id"])
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": rid, "path": "../../etc/passwd", "content": "x", "title": "x"})
    assert r.status_code == 400


def test_publish_endpoint_unknown_repo_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": str(uuid.uuid4()), "path": "x.md", "content": "x", "title": "x"})
    assert r.status_code == 404


def test_publish_endpoint_no_connection_400(client, bob):
    # bob has a workspace but no GithubConnection/repo.
    h = auth_headers(bob["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": str(uuid.uuid4()), "path": "x.md", "content": "x", "title": "x"})
    # Unknown repo resolves first (404); the point is it never 500s.
    assert r.status_code in (400, 404)


# ---------- classic GitHubIntegration (PAT) publish support ---------- #


def _connect_integration(workspace_id: str) -> str:
    """Add a classic GitHubIntegration (the /github PAT flow). Returns its id."""
    iid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(GitHubIntegration(
            id=iid, workspace_id=workspace_id, repo_owner="acme", repo_name="site",
            branch="main", encrypted_pat=secrets_crypto.encrypt("ghp-classic"),
            encrypted_webhook_secret=secrets_crypto.encrypt("wh"),
            is_active=True, created_at=_now(), updated_at=_now()))
    return iid


def test_publish_via_classic_integration(client, alice, monkeypatch):
    """A workspace connected via the classic PAT form (GitHubIntegration,
    no GithubConnection) can still publish."""
    iid = _connect_integration(alice["workspace"]["id"])
    captured = {}
    monkeypatch.setattr(github_publish, "publish_page_to_repo",
                        lambda **k: captured.update(k) or {
                            "pr_url": "https://github.com/acme/site/pull/5",
                            "pr_number": 5, "branch": k["branch_name"]})
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": iid, "title": "Hi", "content": "<h1>x</h1>", "path": "p/x.html"})
    assert r.status_code == 200, r.text
    assert r.json()["pr_number"] == 5
    # Used the integration's repo + its own PAT.
    assert captured["owner"] == "acme" and captured["repo"] == "site"


def test_connection_get_surfaces_integration_repo(client, alice):
    """The /github/connection response (which feeds the /seo repo dropdown)
    includes a classic integration repo so it's selectable for publishing."""
    _connect_integration(alice["workspace"]["id"])
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/github/connection", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    names = [(x["repo_owner"], x["repo_name"]) for x in body["repos"]]
    assert ("acme", "site") in names
    # Reports connected (so /seo doesn't show "connect GitHub").
    assert body["connection"] is not None


def test_integration_repo_prefers_oauth_token_when_present(client, alice, monkeypatch):
    """After connecting via OAuth, an existing classic-integration repo
    publishes with the OAuth (write) token, not the old read-only PAT."""
    iid = _connect_integration(alice["workspace"]["id"])  # integration w/ "ghp-classic"
    # Add an OAuth connection (no GithubRepo) — its token should win.
    with session_scope() as s:
        s.add(GithubConnection(id=str(uuid.uuid4()), workspace_id=alice["workspace"]["id"],
                               encrypted_token=secrets_crypto.encrypt("ghu-oauth-write"),
                               auth_kind="oauth", created_at=_now(), updated_at=_now()))
    captured = {}
    monkeypatch.setattr(github_publish, "publish_page_to_repo",
                        lambda **k: captured.update(k) or {
                            "pr_url": "u", "pr_number": 1, "branch": k["branch_name"]})
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/github/publish-page", headers=h, json={
        "repo_id": iid, "title": "x", "content": "<h1>x</h1>", "path": "p/x.html"})
    assert r.status_code == 200, r.text
    assert captured["token"] == "ghu-oauth-write"  # OAuth token used, not the PAT


# ---------- template fetch for component mode ---------- #


def test_fetch_file_decodes_base64():
    import base64
    fake = _FakeGithub({
        "/contents/src/pages/Foo.tsx": {"status": 200, "body": {
            "encoding": "base64",
            "content": base64.b64encode(b"export default function Foo(){}").decode()}}})
    out = github_publish.fetch_file(request=fake, token="t", owner="o", repo="r",
                                    path="src/pages/Foo.tsx")
    assert out == "export default function Foo(){}"


def test_fetch_file_unsafe_path_none():
    assert github_publish.fetch_file(request=_FakeGithub({}), token="t", owner="o",
                                     repo="r", path="../secret") is None


def test_list_page_files_filters_to_pages():
    fake = _FakeGithub({"/git/trees/HEAD?recursive=1": {"status": 200, "body": {"tree": [
        {"type": "blob", "path": "src/pages/FaqPage.tsx"},
        {"type": "blob", "path": "src/components/Button.tsx"},
        {"type": "blob", "path": "src/data/blog.ts"},
        {"type": "blob", "path": "routes/about.jsx"},
        {"type": "blob", "path": "README.md"},
    ]}}})
    files = github_publish.list_page_files(request=fake, token="t", owner="o", repo="r")
    assert "src/pages/FaqPage.tsx" in files
    assert "routes/about.jsx" in files
    assert "src/components/Button.tsx" not in files  # not a page
    assert "README.md" not in files


def test_design_format_component_mode_fetches_template(client, alice, monkeypatch):
    rid = _connect_integration(alice["workspace"]["id"])  # repo "acme/site"
    import github_publish
    monkeypatch.setattr(github_publish, "fetch_file",
                        lambda **k: "import Layout from './Layout'; export default function Old(){}")
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h, json={
        "content": "<h1>New page</h1>", "template_repo_id": rid,
        "template_path": "src/pages/Old.tsx"})
    assert r.status_code == 200, r.text
    assert r.json()["matched_site"] == "src/pages/Old.tsx"
    cid = r.json()["command_id"]
    with session_scope() as s:
        row = s.execute(text("SELECT payload FROM commands WHERE id=:id"),
                        {"id": cid}).mappings().first()
        assert row["payload"]["content_type"] == "component"
        assert "import Layout" in row["payload"]["template"]


def test_design_format_component_template_unreadable_400(client, alice, monkeypatch):
    rid = _connect_integration(alice["workspace"]["id"])
    import github_publish
    monkeypatch.setattr(github_publish, "fetch_file", lambda **k: None)
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/design/format", headers=h, json={
        "content": "x", "template_repo_id": rid, "template_path": "nope.tsx"})
    assert r.status_code == 400


def test_page_files_endpoint(client, alice, monkeypatch):
    rid = _connect_integration(alice["workspace"]["id"])
    import github_publish
    monkeypatch.setattr(github_publish, "list_page_files",
                        lambda **k: ["src/pages/A.tsx", "src/pages/B.tsx"])
    h = auth_headers(alice["session_token"])
    r = client.get(f"/workspaces/me/github/repos/{rid}/page-files", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["files"] == ["src/pages/A.tsx", "src/pages/B.tsx"]
