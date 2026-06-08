"""Phase 10B.3b: webhook cutover to github_repos (dual-read).

Sets up github_connections + github_repos directly (no legacy
github_integrations row) and verifies the webhook resolves + authenticates
a push via the new model, including the multi-workspace-same-repo case
that the signature-based selection exists to handle.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import secrets_crypto
from db import session_scope
from models import GithubConnection, GithubRepo, Workspace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_repo(*, owner: str, name: str, secret: str, branch: str = "main", active: bool = True) -> str:
    """Create a workspace + connection + repo with `secret` as the
    (encrypted) webhook secret. Returns the workspace_id."""
    ws = str(uuid.uuid4()); cid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=ws, name=f"ws-{ws[:8]}", created_at=_now()))
        s.flush()
        s.add(GithubConnection(
            id=cid, workspace_id=ws, encrypted_token=secrets_crypto.encrypt("ghu_tok"),
            auth_kind="oauth", github_login="octocat", created_at=_now(), updated_at=_now(),
        ))
        s.add(GithubRepo(
            id=str(uuid.uuid4()), workspace_id=ws, connection_id=cid,
            repo_owner=owner, repo_name=name, branch=branch,
            encrypted_webhook_secret=secrets_crypto.encrypt(secret),
            is_active=active, created_at=_now(), updated_at=_now(),
        ))
    return ws


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push_body(owner: str, name: str, *, branch: str = "main") -> bytes:
    return json.dumps({
        "ref": f"refs/heads/{branch}",
        "after": "a" * 40,
        "head_commit": {"id": "a" * 40, "author": {"name": "Dev"}},
        "commits": [{"id": "a" * 40, "added": [], "modified": ["x.py"], "removed": []}],
        "repository": {"full_name": f"{owner}/{name}"},
    }).encode()


def _post(client, body: bytes, *, sig: str, event: str = "push"):
    return client.post(
        "/webhooks/github", content=body,
        headers={"x-github-event": event, "x-hub-signature-256": sig,
                 "x-github-delivery": str(uuid.uuid4()), "content-type": "application/json"},
    )


def test_signed_push_via_github_repos_is_accepted(client):
    secret = "whsec-aaa"
    _make_repo(owner="acme", name="app", secret=secret)
    body = _push_body("acme", "app")
    r = _post(client, body, sig=_sign(secret, body))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


def test_wrong_secret_via_github_repos_is_401(client):
    _make_repo(owner="acme", name="api", secret="real-secret")
    body = _push_body("acme", "api")
    r = _post(client, body, sig=_sign("wrong-secret", body))
    assert r.status_code == 401


def test_unknown_repo_is_404(client):
    body = _push_body("nobody", "nothing")
    r = _post(client, body, sig=_sign("x", body))
    assert r.status_code == 404


def test_inactive_repo_is_skipped(client):
    secret = "whsec-inactive"
    _make_repo(owner="acme", name="off", secret=secret, active=False)
    body = _push_body("acme", "off")
    r = _post(client, body, sig=_sign(secret, body))
    assert r.status_code == 200
    assert r.json().get("skipped") == "integration_inactive"


def test_two_workspaces_same_repo_signature_selects_correct_one(client):
    # Both watch acme/shared but with different webhook secrets.
    _make_repo(owner="acme", name="shared", secret="secret-A", branch="main")
    _make_repo(owner="acme", name="shared", secret="secret-B", branch="release")
    body = _push_body("acme", "shared", branch="release")
    # Sign with B's secret -> resolves to B (branch 'release' matches, tracked).
    r = _post(client, body, sig=_sign("secret-B", body))
    assert r.status_code == 200
    assert r.json().get("skipped") != "branch_not_tracked"
    # Sign with A's secret against a 'release' push -> resolves to A, whose
    # tracked branch is 'main', so it's skipped as not-tracked.
    r2 = _post(client, body, sig=_sign("secret-A", body))
    assert r2.status_code == 200
    assert r2.json().get("skipped") == "branch_not_tracked"
