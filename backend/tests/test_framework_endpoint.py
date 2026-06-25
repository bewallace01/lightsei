"""GET /workspaces/me/github/repos/{id}/framework — sniff a connected repo's
framework so the publish UI can default the page format. The detection logic is
covered purely in test_framework_detect.py; here we assert the endpoint wires
package.json + the page-file list into it and returns the suggested format."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import github_publish
import secrets_crypto
from db import session_scope
from models import GithubConnection, GithubRepo
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect_repo(workspace_id: str) -> str:
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


def test_framework_endpoint_detects_next_app(client, alice, monkeypatch):
    rid = _connect_repo(alice["workspace"]["id"])
    monkeypatch.setattr(github_publish, "fetch_file",
                        lambda **k: json.dumps({"dependencies": {"next": "14.0.0"}}))
    monkeypatch.setattr(github_publish, "list_page_files",
                        lambda **k: ["app/blog/page.tsx", "app/layout.tsx"])
    h = auth_headers(alice["session_token"])
    r = client.get(f"/workspaces/me/github/repos/{rid}/framework", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"framework": "next-app", "suggested_format": "next-app"}


def test_framework_endpoint_static_when_no_package_json(client, alice, monkeypatch):
    rid = _connect_repo(alice["workspace"]["id"])
    monkeypatch.setattr(github_publish, "fetch_file", lambda **k: None)
    monkeypatch.setattr(github_publish, "list_page_files",
                        lambda **k: ["public/index.html"])
    h = auth_headers(alice["session_token"])
    r = client.get(f"/workspaces/me/github/repos/{rid}/framework", headers=h)
    assert r.status_code == 200
    assert r.json() == {"framework": "static", "suggested_format": None}


def test_framework_endpoint_unknown_repo_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get(f"/workspaces/me/github/repos/{uuid.uuid4()}/framework", headers=h)
    assert r.status_code == 404
