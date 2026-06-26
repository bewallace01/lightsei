"""Spica auto-opens the publish PR (Phase 37.10): the DB-bound surface.

Covers the GET/PATCH /workspaces/me/seo/autopublish settings and the
post_event ingest hook that schedules a publish on a seo.page_drafted event.
The publish orchestration itself is stubbed (covered purely in
test_seo_autopublish.py); here we assert the wiring: the gate, the validation,
and that the background task is (or isn't) scheduled.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import main
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


def _page_event_body() -> dict:
    return {
        "run_id": str(uuid.uuid4()),
        "agent_name": "seo",
        "kind": "seo.page_drafted",
        "payload": {
            "command_id": str(uuid.uuid4()),
            "keyword": "emergency plumber austin",
            "page": {"title": "Emergency Plumber Austin", "meta_description": "m",
                     "slug": "emergency-plumber-austin", "h1": "Emergency Plumber",
                     "body_html": "<p>We fix it.</p>"},
        },
    }


# ---------- settings GET/PATCH ---------- #


def test_autopublish_defaults_off(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/seo/autopublish", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "repo_id": None}


def test_autopublish_set_repo_then_enable(client, alice):
    rid = _connect_repo(alice["workspace"]["id"])
    h = auth_headers(alice["session_token"])
    r = client.patch("/workspaces/me/seo/autopublish", headers=h,
                     json={"repo_id": rid, "enabled": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "repo_id": rid}
    # Persisted.
    assert client.get("/workspaces/me/seo/autopublish", headers=h).json() == {
        "enabled": True, "repo_id": rid}


def test_autopublish_enable_without_repo_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch("/workspaces/me/seo/autopublish", headers=h,
                     json={"enabled": True})
    assert r.status_code == 400


def test_autopublish_unknown_repo_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch("/workspaces/me/seo/autopublish", headers=h,
                     json={"repo_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_autopublish_clear_repo_disables_safely(client, alice):
    rid = _connect_repo(alice["workspace"]["id"])
    h = auth_headers(alice["session_token"])
    client.patch("/workspaces/me/seo/autopublish", headers=h,
                 json={"repo_id": rid, "enabled": True})
    # Clearing the repo while enabled is rejected (can't be armed with no target).
    r = client.patch("/workspaces/me/seo/autopublish", headers=h,
                     json={"repo_id": ""})
    assert r.status_code == 400
    # Disable first, then clearing the repo is fine.
    client.patch("/workspaces/me/seo/autopublish", headers=h, json={"enabled": False})
    r = client.patch("/workspaces/me/seo/autopublish", headers=h, json={"repo_id": ""})
    assert r.status_code == 200 and r.json() == {"enabled": False, "repo_id": None}


# ---------- ingest hook ---------- #


def test_page_drafted_schedules_publish_when_enabled(client, alice, monkeypatch):
    rid = _connect_repo(alice["workspace"]["id"])
    hs = auth_headers(alice["session_token"])
    client.patch("/workspaces/me/seo/autopublish", headers=hs,
                 json={"repo_id": rid, "enabled": True})

    calls = []
    monkeypatch.setattr(main, "_autopublish_seo_draft",
                        lambda ws, repo, page: calls.append((ws, repo, page)))

    hk = auth_headers(alice["api_key"]["plaintext"])
    r = client.post("/events", headers=hk, json=_page_event_body())
    assert r.status_code == 200, r.text
    # TestClient runs background tasks after the response.
    assert len(calls) == 1
    ws, repo, page = calls[0]
    assert ws == alice["workspace"]["id"] and repo == rid
    assert page["title"] == "Emergency Plumber Austin"


def test_page_drafted_no_publish_when_disabled(client, alice, monkeypatch):
    # Repo set but auto-publish left off -> no scheduling.
    rid = _connect_repo(alice["workspace"]["id"])
    hs = auth_headers(alice["session_token"])
    client.patch("/workspaces/me/seo/autopublish", headers=hs, json={"repo_id": rid})

    calls = []
    monkeypatch.setattr(main, "_autopublish_seo_draft",
                        lambda *a: calls.append(a))
    hk = auth_headers(alice["api_key"]["plaintext"])
    assert client.post("/events", headers=hk, json=_page_event_body()).status_code == 200
    assert calls == []


def test_other_event_kinds_never_publish(client, alice, monkeypatch):
    rid = _connect_repo(alice["workspace"]["id"])
    hs = auth_headers(alice["session_token"])
    client.patch("/workspaces/me/seo/autopublish", headers=hs,
                 json={"repo_id": rid, "enabled": True})

    calls = []
    monkeypatch.setattr(main, "_autopublish_seo_draft", lambda *a: calls.append(a))
    hk = auth_headers(alice["api_key"]["plaintext"])
    body = {**_page_event_body(), "kind": "seo.audit_complete"}
    assert client.post("/events", headers=hk, json=body).status_code == 200
    assert calls == []


# ---------- 38.2b: auto-publish renders to the repo's framework ---------- #


def test_autopublish_uses_detected_framework_format(client, alice, monkeypatch):
    import github_publish
    import seo_autopublish
    rid = _connect_repo(alice["workspace"]["id"])
    # Simulate a Next.js App Router repo.
    monkeypatch.setattr(github_publish, "fetch_file",
                        lambda **k: '{"dependencies": {"next": "14"}}')
    monkeypatch.setattr(github_publish, "list_page_files",
                        lambda **k: ["app/blog/page.tsx"])
    captured = {}
    monkeypatch.setattr(seo_autopublish, "orchestrate_publish",
                        lambda **k: captured.update(k) or {"pr_url": "x"})
    main._autopublish_seo_draft(alice["workspace"]["id"], rid,
                                {"title": "T", "body_html": "<p>x</p>"})
    assert captured["fmt"] == "next-app"


def test_autopublish_falls_back_to_html_when_detection_fails(client, alice, monkeypatch):
    import github_publish
    import seo_autopublish
    rid = _connect_repo(alice["workspace"]["id"])

    def _boom(**k):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(github_publish, "fetch_file", _boom)
    captured = {}
    monkeypatch.setattr(seo_autopublish, "orchestrate_publish",
                        lambda **k: captured.update(k) or {"pr_url": "x"})
    main._autopublish_seo_draft(alice["workspace"]["id"], rid,
                                {"title": "T", "body_html": "<p>x</p>"})
    assert captured["fmt"] == "html"
