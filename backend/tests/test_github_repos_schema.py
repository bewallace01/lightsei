"""Phase 10B: github_connections + github_repos schema.

Verifies the 0049 migration applied (conftest migrates to head) and the
new models round-trip with their constraints + cascades. The backfill
from github_integrations runs at migration time against prod's existing
row; the test DB starts empty, so this covers the table shapes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import GithubConnection, GithubRepo, Workspace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ws() -> str:
    wid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=wid, name=f"ws-{wid[:8]}", created_at=_now()))
    return wid


def _conn(ws_id) -> str:
    cid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(GithubConnection(
            id=cid, workspace_id=ws_id, encrypted_token="enc-tok",
            auth_kind="oauth", github_login="octocat",
            created_at=_now(), updated_at=_now(),
        ))
    return cid


def _repo(ws_id, conn_id, *, owner="acme", name="app"):
    return GithubRepo(
        id=str(uuid.uuid4()), workspace_id=ws_id, connection_id=conn_id,
        repo_owner=owner, repo_name=name, branch="main",
        encrypted_webhook_secret="enc-wh", is_active=True,
        created_at=_now(), updated_at=_now(),
    )


def test_connection_and_repo_round_trip():
    ws = _ws(); cid = _conn(ws)
    with session_scope() as s:
        s.add(_repo(ws, cid, owner="acme", name="api"))
    with session_scope() as s:
        c = s.get(GithubConnection, cid)
        assert c.auth_kind == "oauth" and c.github_login == "octocat"
        r = s.execute(select(GithubRepo).where(GithubRepo.connection_id == cid)).scalar_one()
        assert (r.repo_owner, r.repo_name, r.branch) == ("acme", "api", "main")


def test_one_connection_per_workspace():
    ws = _ws(); _conn(ws)
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(GithubConnection(
                id=str(uuid.uuid4()), workspace_id=ws, encrypted_token="t2",
                auth_kind="pat", created_at=_now(), updated_at=_now(),
            ))


def test_multiple_repos_per_workspace():
    ws = _ws(); cid = _conn(ws)
    with session_scope() as s:
        s.add(_repo(ws, cid, name="repo-a"))
        s.add(_repo(ws, cid, name="repo-b"))
    with session_scope() as s:
        n = len(s.execute(select(GithubRepo).where(GithubRepo.workspace_id == ws)).scalars().all())
        assert n == 2


def test_unique_repo_per_workspace():
    ws = _ws(); cid = _conn(ws)
    with session_scope() as s:
        s.add(_repo(ws, cid, owner="acme", name="dup"))
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(_repo(ws, cid, owner="acme", name="dup"))


def test_same_repo_allowed_in_two_workspaces():
    ws1 = _ws(); c1 = _conn(ws1)
    ws2 = _ws(); c2 = _conn(ws2)
    with session_scope() as s:
        s.add(_repo(ws1, c1, owner="shared", name="repo"))
    with session_scope() as s:  # different workspace, same repo name: allowed
        s.add(_repo(ws2, c2, owner="shared", name="repo"))


def test_cascade_on_connection_delete():
    ws = _ws(); cid = _conn(ws)
    with session_scope() as s:
        s.add(_repo(ws, cid))
    with session_scope() as s:
        s.delete(s.get(GithubConnection, cid))
    with session_scope() as s:
        assert s.execute(select(GithubRepo).where(GithubRepo.connection_id == cid)).first() is None
