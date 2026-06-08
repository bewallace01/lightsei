"""Phase 10B.2: GitHub OAuth connect + multi-repo endpoints.

Drives the real endpoints via TestClient, stubbing the GitHub network
calls (token exchange, /user, repo validation).
"""
from __future__ import annotations

import types

import pytest

import github_oauth
import github_api
from db import session_scope
from models import GithubConnection, GithubRepo
from tests.conftest import auth_headers, signup


@pytest.fixture()
def _configured(monkeypatch):
    monkeypatch.setenv("LIGHTSEI_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("LIGHTSEI_GITHUB_CLIENT_SECRET", "csec")


def _S(acct):
    return auth_headers(acct["session_token"])


# ---------- oauth/start ---------- #


def test_start_503_when_not_configured(client, alice, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("LIGHTSEI_GITHUB_CLIENT_SECRET", raising=False)
    r = client.get("/workspaces/me/github/oauth/start", headers=_S(alice))
    assert r.status_code == 503
    assert "LIGHTSEI_GITHUB_CLIENT_ID" in r.json()["detail"]


def test_start_returns_url_and_persists_state(client, alice, _configured):
    r = client.get("/workspaces/me/github/oauth/start", headers=_S(alice))
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_url"].startswith("https://github.com/login/oauth/authorize")
    assert body["state"] in body["authorization_url"]


# ---------- oauth/callback ---------- #


def _prime_state(client, alice, _configured) -> str:
    return client.get("/workspaces/me/github/oauth/start", headers=_S(alice)).json()["state"]


def test_callback_stores_connection_and_redirects(client, alice, _configured, monkeypatch):
    state = _prime_state(client, alice, _configured)
    monkeypatch.setattr(github_oauth, "exchange_code_for_token", lambda **k: "ghu_tok123")

    def fake_get(url, **kwargs):
        return types.SimpleNamespace(status_code=200, json=lambda: {"login": "octocat"})
    monkeypatch.setattr("httpx.get", fake_get)

    r = client.get(f"/github/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        conn = s.execute(
            __import__("sqlalchemy").select(GithubConnection).where(
                GithubConnection.workspace_id == alice["workspace"]["id"]
            )
        ).scalar_one()
        assert conn.auth_kind == "oauth"
        assert conn.github_login == "octocat"


def test_callback_bad_state_is_400_html(client):
    r = client.get("/github/oauth/callback?code=abc&state=nope", follow_redirects=False)
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]


def test_callback_error_param_is_400_html(client):
    r = client.get("/github/oauth/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 400


def test_callback_state_is_single_use(client, alice, _configured, monkeypatch):
    state = _prime_state(client, alice, _configured)
    monkeypatch.setattr(github_oauth, "exchange_code_for_token", lambda **k: "ghu_tok")
    monkeypatch.setattr("httpx.get", lambda url, **k: types.SimpleNamespace(status_code=500, json=lambda: {}))
    first = client.get(f"/github/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert first.status_code == 303
    second = client.get(f"/github/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert second.status_code == 400  # state was consumed


# ---------- repos ---------- #


def _connect(client, alice, _configured, monkeypatch):
    state = _prime_state(client, alice, _configured)
    monkeypatch.setattr(github_oauth, "exchange_code_for_token", lambda **k: "ghu_tok")
    monkeypatch.setattr("httpx.get", lambda url, **k: types.SimpleNamespace(status_code=200, json=lambda: {"login": "o"}))
    client.get(f"/github/oauth/callback?code=abc&state={state}", follow_redirects=False)


def test_add_repo_requires_connection(client, alice):
    r = client.post("/workspaces/me/github/repos", headers=_S(alice),
                    json={"repo_owner": "acme", "repo_name": "app"})
    assert r.status_code == 400
    assert "connect GitHub first" in r.json()["detail"]


def test_add_repo_creates_row_and_returns_secret_once(client, alice, _configured, monkeypatch):
    _connect(client, alice, _configured, monkeypatch)
    monkeypatch.setattr(github_api, "validate_pat", lambda **k: types.SimpleNamespace(default_branch="main"))
    r = client.post("/workspaces/me/github/repos", headers=_S(alice),
                    json={"repo_owner": "acme", "repo_name": "app", "branch": "main"})
    assert r.status_code == 200
    body = r.json()
    assert body["repo_owner"] == "acme"
    assert "webhook_secret" in body and body["webhook_secret"]
    # Re-add is idempotent and does NOT re-reveal the secret.
    r2 = client.post("/workspaces/me/github/repos", headers=_S(alice),
                     json={"repo_owner": "acme", "repo_name": "app", "branch": "develop"})
    assert r2.status_code == 200
    assert "webhook_secret" not in r2.json()
    assert r2.json()["branch"] == "develop"


def test_connection_endpoint_lists_repos(client, alice, _configured, monkeypatch):
    _connect(client, alice, _configured, monkeypatch)
    monkeypatch.setattr(github_api, "validate_pat", lambda **k: types.SimpleNamespace(default_branch="main"))
    client.post("/workspaces/me/github/repos", headers=_S(alice),
                json={"repo_owner": "acme", "repo_name": "a"})
    client.post("/workspaces/me/github/repos", headers=_S(alice),
                json={"repo_owner": "acme", "repo_name": "b"})
    r = client.get("/workspaces/me/github/connection", headers=_S(alice))
    assert r.status_code == 200
    body = r.json()
    assert body["connection"]["auth_kind"] == "oauth"
    assert {x["repo_name"] for x in body["repos"]} == {"a", "b"}


def test_delete_repo_scoped(client, alice, bob, _configured, monkeypatch):
    _connect(client, alice, _configured, monkeypatch)
    monkeypatch.setattr(github_api, "validate_pat", lambda **k: types.SimpleNamespace(default_branch="main"))
    rid = client.post("/workspaces/me/github/repos", headers=_S(alice),
                      json={"repo_owner": "acme", "repo_name": "x"}).json()["id"]
    # bob can't delete alice's repo
    assert client.delete(f"/workspaces/me/github/repos/{rid}", headers=_S(bob)).status_code == 404
    assert client.delete(f"/workspaces/me/github/repos/{rid}", headers=_S(alice)).status_code == 200
