"""Phase 10B.4: per-env branch -> agent routing.

Covers the branch-target CRUD endpoints and the webhook routing: a repo
with branch targets deploys only the push branch's mapped agents; a repo
with none keeps the legacy single-branch behavior.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import github_api
import secrets_crypto
from db import session_scope
from models import (
    GitHubAgentPath,
    GithubConnection,
    GithubRepo,
    GithubRepoBranchTarget,
    Workspace,
)
from tests.conftest import auth_headers, signup


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push(owner, name, *, branch, touched="backend/x.py") -> bytes:
    return json.dumps({
        "ref": f"refs/heads/{branch}",
        "after": "a" * 40,
        "head_commit": {"id": "a" * 40, "author": {"name": "Dev"}},
        "commits": [{"id": "a" * 40, "added": [], "modified": [touched], "removed": []}],
        "repository": {"full_name": f"{owner}/{name}"},
    }).encode()


def _post(client, body, *, sig):
    return client.post(
        "/webhooks/github", content=body,
        headers={"x-github-event": "push", "x-hub-signature-256": sig,
                 "x-github-delivery": str(uuid.uuid4()), "content-type": "application/json"},
    )


def _setup_repo(*, owner, name, secret, branch="main") -> tuple[str, str]:
    """workspace + connection + repo. Returns (workspace_id, repo_id)."""
    ws = str(uuid.uuid4()); cid = str(uuid.uuid4()); rid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=ws, name=f"ws-{ws[:8]}", created_at=_now()))
        s.flush()
        s.add(GithubConnection(id=cid, workspace_id=ws, encrypted_token=secrets_crypto.encrypt("ghu"),
                               auth_kind="oauth", created_at=_now(), updated_at=_now()))
        s.add(GithubRepo(id=rid, workspace_id=ws, connection_id=cid, repo_owner=owner,
                         repo_name=name, branch=branch,
                         encrypted_webhook_secret=secrets_crypto.encrypt(secret),
                         is_active=True, created_at=_now(), updated_at=_now()))
    return ws, rid


def _agent_path(ws, agent, path="backend/"):
    with session_scope() as s:
        s.add(GitHubAgentPath(workspace_id=ws, agent_name=agent, path=path,
                              created_at=_now(), updated_at=_now()))


def _branch_target(ws, rid, branch, agent):
    with session_scope() as s:
        s.add(GithubRepoBranchTarget(id=str(uuid.uuid4()), repo_id=rid, workspace_id=ws,
                                     branch=branch, agent_name=agent, created_at=_now()))


# ---------- webhook per-env routing ---------- #


def test_push_routes_to_branch_mapped_agents(client, monkeypatch):
    monkeypatch.setattr(github_api, "fetch_directory_zip", lambda **k: b"zip")
    secret = "whsec1"
    ws, rid = _setup_repo(owner="acme", name="svc", secret=secret)
    _agent_path(ws, "atlas")
    _agent_path(ws, "atlas-staging")
    _branch_target(ws, rid, "main", "atlas")
    _branch_target(ws, rid, "staging", "atlas-staging")

    # Push to main -> only atlas.
    body = _push("acme", "svc", branch="main")
    r = _post(client, body, sig=_sign(secret, body))
    assert r.status_code == 200
    assert {q["agent_name"] for q in r.json()["queued_redeploys"]} == {"atlas"}

    # Push to staging -> only atlas-staging.
    body = _push("acme", "svc", branch="staging")
    r = _post(client, body, sig=_sign(secret, body))
    assert {q["agent_name"] for q in r.json()["queued_redeploys"]} == {"atlas-staging"}


def test_push_to_unmapped_branch_is_skipped(client):
    secret = "whsec2"
    ws, rid = _setup_repo(owner="acme", name="svc2", secret=secret)
    _branch_target(ws, rid, "main", "atlas")
    body = _push("acme", "svc2", branch="feature-x")
    r = _post(client, body, sig=_sign(secret, body))
    assert r.status_code == 200
    assert r.json()["skipped"] == "branch_not_tracked"


def test_repo_without_targets_uses_legacy_branch(client, monkeypatch):
    monkeypatch.setattr(github_api, "fetch_directory_zip", lambda **k: b"zip")
    secret = "whsec3"
    ws, rid = _setup_repo(owner="acme", name="svc3", secret=secret, branch="main")
    _agent_path(ws, "atlas")  # no branch targets -> legacy: all touched agents on repo.branch
    body = _push("acme", "svc3", branch="main")
    r = _post(client, body, sig=_sign(secret, body))
    assert {q["agent_name"] for q in r.json()["queued_redeploys"]} == {"atlas"}
    # A push to a non-tracked branch is skipped under legacy too.
    body = _push("acme", "svc3", branch="other")
    r = _post(client, body, sig=_sign(secret, body))
    assert r.json()["skipped"] == "branch_not_tracked"


# ---------- branch-target endpoints ---------- #


def _connect_and_repo(client, acct) -> str:
    """Create a connection + repo for the signed-up account via the DB,
    return repo_id (the endpoints just need an owned GithubRepo)."""
    ws = acct["workspace"]["id"]
    cid = str(uuid.uuid4()); rid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(GithubConnection(id=cid, workspace_id=ws, encrypted_token=secrets_crypto.encrypt("t"),
                               auth_kind="oauth", created_at=_now(), updated_at=_now()))
        s.add(GithubRepo(id=rid, workspace_id=ws, connection_id=cid, repo_owner="o", repo_name="n",
                         branch="main", encrypted_webhook_secret=secrets_crypto.encrypt("w"),
                         is_active=True, created_at=_now(), updated_at=_now()))
    return rid


def test_branch_target_crud(client, alice):
    rid = _connect_and_repo(client, alice)
    h = auth_headers(alice["session_token"])
    r = client.post(f"/workspaces/me/github/repos/{rid}/branch-targets", headers=h,
                    json={"branch": "main", "agent_name": "atlas"})
    assert r.status_code == 200
    tid = r.json()["id"]
    # Idempotent add.
    assert client.post(f"/workspaces/me/github/repos/{rid}/branch-targets", headers=h,
                       json={"branch": "main", "agent_name": "atlas"}).json()["id"] == tid
    # List.
    lst = client.get(f"/workspaces/me/github/repos/{rid}/branch-targets", headers=h).json()["branch_targets"]
    assert len(lst) == 1 and lst[0]["agent_name"] == "atlas"
    # Delete.
    assert client.delete(f"/workspaces/me/github/repos/{rid}/branch-targets/{tid}", headers=h).status_code == 200
    assert client.get(f"/workspaces/me/github/repos/{rid}/branch-targets", headers=h).json()["branch_targets"] == []


def test_branch_target_workspace_scoped(client, alice, bob):
    rid = _connect_and_repo(client, alice)
    # bob can't add a target to alice's repo.
    r = client.post(f"/workspaces/me/github/repos/{rid}/branch-targets",
                    headers=auth_headers(bob["session_token"]),
                    json={"branch": "main", "agent_name": "atlas"})
    assert r.status_code == 404
