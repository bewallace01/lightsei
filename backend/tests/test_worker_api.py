"""Phase 5.2: worker-facing endpoints (claim, status, heartbeat, logs,
blob fetch, secrets fetch)."""
import io

import pytest

from tests.conftest import auth_headers


WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}
WRONG_HEADERS = {"Authorization": "Bearer wrong-token"}


def _upload(client, h, agent="my-bot", payload=b"PK\x03\x04hi"):
    r = client.post(
        "/workspaces/me/deployments",
        headers=h,
        data={"agent_name": agent},
        files={"bundle": ("b.zip", io.BytesIO(payload), "application/zip")},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------- auth ----------

def test_worker_endpoints_require_token(client):
    r = client.post("/worker/deployments/claim?worker_id=w1")
    assert r.status_code == 401

    r = client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WRONG_HEADERS,
    )
    assert r.status_code == 401


def test_worker_endpoints_503_without_token_env(client, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_WORKER_TOKEN", raising=False)
    r = client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )
    assert r.status_code == 503


# ---------- claim ----------

def test_claim_returns_none_when_nothing_queued(client):
    r = client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["deployment"] is None


def test_claim_picks_oldest_queued_and_promotes_to_building(client, alice):
    h = auth_headers(alice["session_token"])
    first = _upload(client, h, "first")
    second = _upload(client, h, "second")

    r = client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )
    body = r.json()
    assert body["deployment"]["id"] == first["id"]
    assert body["deployment"]["status"] == "building"
    assert body["deployment"]["claimed_by"] == "w1"
    assert body["workspace_id"] == alice["workspace"]["id"]

    # A second claim picks up the next one.
    r = client.post(
        "/worker/deployments/claim?worker_id=w2", headers=WORKER_HEADERS,
    )
    assert r.json()["deployment"]["id"] == second["id"]


def test_claim_skips_already_claimed_with_fresh_heartbeat(client, alice):
    h = auth_headers(alice["session_token"])
    _upload(client, h, "only-one")

    r1 = client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )
    assert r1.json()["deployment"]["claimed_by"] == "w1"

    # No second claim available — first worker still owns it.
    r2 = client.post(
        "/worker/deployments/claim?worker_id=w2", headers=WORKER_HEADERS,
    )
    assert r2.json()["deployment"] is None


def test_claim_steals_when_heartbeat_stale(client, alice):
    """A worker with a stale heartbeat is presumed dead; another worker can
    re-claim. Implementation: directly age the heartbeat in the DB."""
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "abandoned")

    # Worker 1 claims.
    client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )

    # Age the heartbeat past the TTL.
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from db import engine
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE deployments SET heartbeat_at = :ts WHERE id = :id"),
            {"ts": stale, "id": dep["id"]},
        )

    # Worker 2 should now be able to claim.
    r = client.post(
        "/worker/deployments/claim?worker_id=w2", headers=WORKER_HEADERS,
    )
    body = r.json()
    assert body["deployment"]["id"] == dep["id"]
    assert body["deployment"]["claimed_by"] == "w2"


def test_claim_skips_stopped_deployments(client, alice):
    """desired_state='stopped' deployments are not claimable."""
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")

    from sqlalchemy import text
    from db import engine
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE deployments SET desired_state = 'stopped' WHERE id = :id"),
            {"id": dep["id"]},
        )

    r = client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )
    assert r.json()["deployment"] is None


# ---------- status ----------

def test_status_running_sets_started_at(client, alice):
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")
    client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )

    r = client.post(
        f"/worker/deployments/{dep['id']}/status",
        headers=WORKER_HEADERS,
        json={"status": "running"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["started_at"] is not None
    assert body["stopped_at"] is None


def test_status_failed_records_error_and_stopped_at(client, alice):
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")

    r = client.post(
        f"/worker/deployments/{dep['id']}/status",
        headers=WORKER_HEADERS,
        json={"status": "failed", "error": "pip install failed: no such package foobar"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"] == "pip install failed: no such package foobar"
    assert body["stopped_at"] is not None


def test_status_unknown_400(client, alice):
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")
    r = client.post(
        f"/worker/deployments/{dep['id']}/status",
        headers=WORKER_HEADERS,
        json={"status": "weird"},
    )
    assert r.status_code == 400


def test_status_unknown_deployment_404(client):
    r = client.post(
        "/worker/deployments/no-such-id/status",
        headers=WORKER_HEADERS,
        json={"status": "running"},
    )
    assert r.status_code == 404


# ---------- heartbeat ----------

def test_heartbeat_advances_and_returns_deployment(client, alice):
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")
    client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )

    r1 = client.get(f"/workspaces/me/deployments/{dep['id']}", headers=h).json()
    import time; time.sleep(0.05)
    r = client.post(
        f"/worker/deployments/{dep['id']}/heartbeat", headers=WORKER_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    # Must return the deployment so the worker can see desired_state changes.
    assert body["id"] == dep["id"]
    assert body["desired_state"] == "running"
    assert body["heartbeat_at"] > r1["heartbeat_at"]


def test_heartbeat_409_on_failed_status(client, alice):
    """Defense in depth (parking-lot #168): worker heartbeats against
    a `failed` deployment row are refused with 409 deployment_terminal.
    Surfaced during the vela investigation (2026-05-23): a worker had
    been heartbeating a failed row for 4 days because nothing here
    enforced the terminal-status invariant."""
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "term-fail")
    client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )

    # Worker reports the bot crashed.
    r_status = client.post(
        f"/worker/deployments/{dep['id']}/status",
        headers=WORKER_HEADERS,
        json={"status": "failed", "error": "bundle fetch 500"},
    )
    assert r_status.status_code == 200

    # Now a follow-up heartbeat should be refused.
    r = client.post(
        f"/worker/deployments/{dep['id']}/heartbeat",
        headers=WORKER_HEADERS,
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "deployment_terminal"
    assert detail["status"] == "failed"
    assert detail["deployment_id"] == dep["id"]


def test_heartbeat_409_on_stopped_status(client, alice):
    """Same defense-in-depth for the `stopped` terminal state."""
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "term-stop")
    client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )

    r_status = client.post(
        f"/worker/deployments/{dep['id']}/status",
        headers=WORKER_HEADERS,
        json={"status": "stopped"},
    )
    assert r_status.status_code == 200

    r = client.post(
        f"/worker/deployments/{dep['id']}/heartbeat",
        headers=WORKER_HEADERS,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "stopped"


def test_heartbeat_still_advances_on_non_terminal_status(client, alice):
    """Building / running / queued (non-terminal) rows MUST still
    accept heartbeats — the defense-in-depth check is scoped to
    `failed` + `stopped` only."""
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "non-term")
    client.post(
        "/worker/deployments/claim?worker_id=w1", headers=WORKER_HEADERS,
    )

    # Promote to running.
    client.post(
        f"/worker/deployments/{dep['id']}/status",
        headers=WORKER_HEADERS,
        json={"status": "running"},
    )

    r = client.post(
        f"/worker/deployments/{dep['id']}/heartbeat",
        headers=WORKER_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "running"


# ---------- logs ----------

def test_log_append_writes_lines(client, alice):
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")
    r = client.post(
        f"/worker/deployments/{dep['id']}/logs",
        headers=WORKER_HEADERS,
        json={"lines": [
            {"stream": "stdout", "line": "starting up"},
            {"stream": "stderr", "line": "warning: deprecated"},
        ]},
    )
    assert r.status_code == 200
    assert r.json()["appended"] == 2

    # Sanity: rows landed.
    from sqlalchemy import text
    from db import engine
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT stream, line FROM deployment_logs "
                "WHERE deployment_id = :id ORDER BY id"
            ),
            {"id": dep["id"]},
        ).all()
    assert len(rows) == 2
    assert rows[0] == ("stdout", "starting up")
    assert rows[1] == ("stderr", "warning: deprecated")


def test_log_append_caps_at_1000(client, alice):
    """When the cap is reached, oldest lines get pruned. Verify by appending
    just past the cap and checking the surviving range."""
    h = auth_headers(alice["session_token"])
    dep = _upload(client, h, "x")

    # First batch of 1000 lines exactly.
    client.post(
        f"/worker/deployments/{dep['id']}/logs",
        headers=WORKER_HEADERS,
        json={"lines": [
            {"stream": "stdout", "line": f"line-{i}"} for i in range(1000)
        ]},
    )

    # Append 5 more — oldest 5 should be pruned.
    client.post(
        f"/worker/deployments/{dep['id']}/logs",
        headers=WORKER_HEADERS,
        json={"lines": [
            {"stream": "stdout", "line": f"new-{i}"} for i in range(5)
        ]},
    )

    from sqlalchemy import text
    from db import engine
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT line FROM deployment_logs "
                "WHERE deployment_id = :id ORDER BY id"
            ),
            {"id": dep["id"]},
        ).all()
    lines = [r[0] for r in rows]
    assert len(lines) == 1000
    # Oldest five are gone.
    assert "line-0" not in lines
    assert "line-4" not in lines
    assert "line-5" in lines  # 6th original survives
    assert lines[-1] == "new-4"


# ---------- blob ----------

def test_blob_fetch_returns_raw_bytes(client, alice):
    h = auth_headers(alice["session_token"])
    payload = b"PK\x03\x04hello-world-zip-or-something"
    dep = _upload(client, h, "x", payload=payload)

    r = client.get(
        f"/worker/blobs/{dep['source_blob_id']}", headers=WORKER_HEADERS,
    )
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"] == "application/octet-stream"
    import hashlib
    assert r.headers["x-lightsei-blob-sha256"] == hashlib.sha256(payload).hexdigest()


def test_blob_404_for_unknown(client):
    r = client.get(
        "/worker/blobs/no-such-id", headers=WORKER_HEADERS,
    )
    assert r.status_code == 404


# ---------- secrets fetch ----------

def test_workspace_secrets_returned_decrypted(client, alice):
    h = auth_headers(alice["session_token"])
    client.put(
        "/workspaces/me/secrets/OPENAI_API_KEY",
        headers=h, json={"value": "sk-test-1"},
    )
    client.put(
        "/workspaces/me/secrets/ANTHROPIC_API_KEY",
        headers=h, json={"value": "sk-ant-1"},
    )

    r = client.get(
        f"/worker/workspaces/{alice['workspace']['id']}/secrets",
        headers=WORKER_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["secrets"] == {
        "OPENAI_API_KEY": "sk-test-1",
        "ANTHROPIC_API_KEY": "sk-ant-1",
    }


def test_workspace_secrets_inject_business_industry(client, alice):
    """Phase 33.3: a completed onboarding profile surfaces the industry as
    LIGHTSEI_BUSINESS_INDUSTRY so deployed personas can tailor their voice."""
    h = auth_headers(alice["session_token"])
    client.post(
        "/workspaces/me/onboarding", headers=h,
        json={"industry": "restaurant", "goals": ["summary"]},
    )
    r = client.get(
        f"/worker/workspaces/{alice['workspace']['id']}/secrets",
        headers=WORKER_HEADERS,
    )
    assert r.json()["secrets"]["LIGHTSEI_BUSINESS_INDUSTRY"] == "restaurant"


def test_workspace_secrets_no_industry_before_onboarding(client, alice):
    r = client.get(
        f"/worker/workspaces/{alice['workspace']['id']}/secrets",
        headers=WORKER_HEADERS,
    )
    assert "LIGHTSEI_BUSINESS_INDUSTRY" not in r.json()["secrets"]


def test_workspace_secrets_503_when_master_key_missing(client, alice, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_SECRETS_KEY", raising=False)
    r = client.get(
        f"/worker/workspaces/{alice['workspace']['id']}/secrets",
        headers=WORKER_HEADERS,
    )
    assert r.status_code == 503
