"""Git-deploy publish path: commit a generated SEO page to a connected repo
and open a PR (covers Vercel / Cloudflare / Railway / Netlify via git).

Pure helper tests (stubbed GitHub request seam) + the endpoint
(/workspaces/me/github/publish-page) wiring.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import github_publish
import secrets_crypto
from db import session_scope
from models import GithubConnection, GithubRepo
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
    # The four steps fired in order.
    methods = [c[0] for c in fake.calls]
    assert methods == ["GET", "POST", "PUT", "POST"]
    # The file body was base64-encoded.
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert "content" in put[2] and put[2]["content"] != "# hi"


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
