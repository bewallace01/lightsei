"""Phase 12C.6.8: tests for the async generation_jobs queue.

Two surfaces under test:

1. The `backend/jobs.py` runner mechanics — state machine, error
   capture, unknown-kind handling, ordering. These rely on the
   in-process runner that FastAPI's lifespan starts; tests enqueue a
   row with a stub handler registered for a private kind, then wait
   for the runner to finalize. Polling cadence is fast (~20ms) and
   the runner's idle sleep is shrunk to 10ms in conftest, so the
   round-trip is sub-100ms in the happy path.

2. The `GET /workspaces/me/generation-jobs/{id}` poll endpoint:
   shape on success, cross-workspace 404, nonexistent-id 404,
   unauthenticated 401.

End-to-end "POST /agents/generate → row → result_payload on poll" is
covered by the existing endpoint tests in test_agent_generator.py and
test_team_planner.py through `kick_and_wait_for_job`; no need to
duplicate that here.
"""
from __future__ import annotations

import time
import uuid

import pytest

import jobs
from db import SessionLocal
from tests.conftest import auth_headers


# ---------- helpers ---------- #


def _enqueue(
    *, workspace_id: str, kind: str, payload: dict | None = None,
) -> str:
    """Insert a pending row via `jobs.enqueue_job` (same path endpoints use)."""
    job_id = str(uuid.uuid4())
    s = SessionLocal()
    try:
        jobs.enqueue_job(
            s,
            job_id=job_id,
            workspace_id=workspace_id,
            kind=kind,
            request_payload=payload or {},
        )
        s.commit()
    finally:
        s.close()
    return job_id


def _read_job(job_id: str) -> dict:
    s = SessionLocal()
    try:
        from sqlalchemy import text
        row = s.execute(
            text(
                "SELECT id, status, result_payload, error, attempt_count, "
                "started_at, finished_at FROM generation_jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).mappings().first()
        return dict(row) if row else {}
    finally:
        s.close()


def _wait_for_terminal(job_id: str, timeout_s: float = 5.0) -> dict:
    """Drive the runner directly (the background runner is disabled in tests,
    so we pump run_one_job() ourselves) until this job reaches a terminal
    state. Deterministic — no dependence on a polling thread."""
    import asyncio

    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _read_job(job_id)
        if last.get("status") in ("success", "failed"):
            return last
        # Process one pending job (this one, or another queued); stop pumping
        # when there's no work left and we still haven't hit terminal.
        did = asyncio.run(jobs.run_one_job())
        if not did and _read_job(job_id).get("status") not in ("success", "failed"):
            time.sleep(0.02)
    last = _read_job(job_id)
    if last.get("status") in ("success", "failed"):
        return last
    raise AssertionError(
        f"job {job_id} did not reach terminal state within {timeout_s}s; last={last}"
    )


# ---------- Runner state machine ---------- #


def test_runner_success_path(client, alice, monkeypatch):
    """Stub handler returns a dict → runner writes status='success' +
    result_payload, sets started_at + finished_at, bumps attempt_count."""
    workspace_id = alice["workspace"]["id"]

    def handler(session, ws_id, payload):
        assert ws_id == workspace_id
        return {"ok": True, "echo": payload.get("x")}

    monkeypatch.setitem(jobs._HANDLERS, "_test_success", handler)
    job_id = _enqueue(
        workspace_id=workspace_id, kind="_test_success", payload={"x": 42}
    )

    row = _wait_for_terminal(job_id)
    assert row["status"] == "success"
    assert row["result_payload"] == {"ok": True, "echo": 42}
    assert row["error"] is None
    assert row["attempt_count"] == 1
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


def test_runner_failure_captures_error(client, alice, monkeypatch):
    """Handler raises → runner writes status='failed' with the exception
    text. result_payload stays null; the row doesn't get re-picked."""
    workspace_id = alice["workspace"]["id"]

    def handler(session, ws_id, payload):
        raise RuntimeError("the handler exploded")

    monkeypatch.setitem(jobs._HANDLERS, "_test_failure", handler)
    job_id = _enqueue(workspace_id=workspace_id, kind="_test_failure")

    row = _wait_for_terminal(job_id)
    assert row["status"] == "failed"
    assert row["result_payload"] is None
    assert "RuntimeError" in (row["error"] or "")
    assert "the handler exploded" in (row["error"] or "")
    assert row["attempt_count"] == 1


def test_runner_unknown_kind_terminal_failure(client, alice):
    """A row with a kind nobody registered shouldn't sit in the queue
    forever. Runner marks it failed with "no handler" text so the
    dashboard's poll surfaces a meaningful error."""
    workspace_id = alice["workspace"]["id"]
    kind = f"_test_never_registered_{uuid.uuid4().hex[:8]}"
    job_id = _enqueue(workspace_id=workspace_id, kind=kind)

    row = _wait_for_terminal(job_id)
    assert row["status"] == "failed"
    assert "no handler" in (row["error"] or "")
    assert kind in (row["error"] or "")


def test_runner_processes_two_jobs_in_order(client, alice, monkeypatch):
    """The claim query is ORDER BY created_at LIMIT 1 — older rows go
    first. Verified by handler-side ordering of the payloads."""
    workspace_id = alice["workspace"]["id"]
    seen: list[str] = []

    def handler(session, ws_id, payload):
        seen.append(payload["tag"])
        return {"tag": payload["tag"]}

    monkeypatch.setitem(jobs._HANDLERS, "_test_order", handler)
    first_id = _enqueue(
        workspace_id=workspace_id, kind="_test_order", payload={"tag": "first"}
    )
    # Ensure a measurable created_at gap so ordering is deterministic.
    time.sleep(0.05)
    second_id = _enqueue(
        workspace_id=workspace_id, kind="_test_order", payload={"tag": "second"}
    )

    _wait_for_terminal(first_id)
    _wait_for_terminal(second_id)
    assert seen == ["first", "second"]


def test_runner_does_not_double_process_a_row(client, alice, monkeypatch):
    """attempt_count is incremented exactly once per row. Confirms the
    claim's "set status='running' atomically" plus the no-auto-retry
    behavior together prevent double-processing."""
    workspace_id = alice["workspace"]["id"]
    invocations: list[int] = []

    def handler(session, ws_id, payload):
        invocations.append(1)
        return {"n": len(invocations)}

    monkeypatch.setitem(jobs._HANDLERS, "_test_no_double", handler)
    job_id = _enqueue(workspace_id=workspace_id, kind="_test_no_double")

    row = _wait_for_terminal(job_id)
    # Wait a little extra in case the runner re-polls after a stray
    # 'pending' visibility blip.
    time.sleep(0.2)
    row_again = _read_job(job_id)
    assert len(invocations) == 1
    assert row_again["attempt_count"] == 1
    assert row_again["status"] == "success"
    assert row["status"] == "success"


def test_claim_pending_job_skip_locked_yields_only_one_winner(client, alice, monkeypatch):
    """Two concurrent claimers should never both succeed on the same
    row. We register a slow handler so the in-process runner is busy on
    something else (or has nothing to do), then call claim_pending_job
    twice from two sessions and assert at most one wins."""
    workspace_id = alice["workspace"]["id"]

    # Use a kind with no handler so the runner won't process this row
    # before our claim attempts (it'll see the row, try to dispatch,
    # find no handler, and mark it failed — but that finalization
    # itself uses a claim, so we just need to win the claim race
    # before the runner does, or accept that one of our claimers loses
    # to the runner).
    kind = f"_test_skip_locked_{uuid.uuid4().hex[:8]}"
    job_id = _enqueue(workspace_id=workspace_id, kind=kind)

    s1 = SessionLocal()
    s2 = SessionLocal()
    try:
        claim_a = jobs.claim_pending_job(s1)
        claim_b = jobs.claim_pending_job(s2)
    finally:
        s1.close()
        s2.close()

    # Exactly one of {claim_a, claim_b, in-process runner} could have
    # claimed the row. From the test's vantage point: if BOTH our claims
    # returned the same row, SKIP LOCKED failed and we have a bug.
    claimed_ids = [c["id"] for c in (claim_a, claim_b) if c is not None]
    assert len(claimed_ids) == len(set(claimed_ids)), (
        f"two claimers got the same row: {claimed_ids}"
    )
    # And at most one of our two claims should be this specific job.
    matches = [cid for cid in claimed_ids if cid == job_id]
    assert len(matches) <= 1


# ---------- Poll endpoint ---------- #


def test_poll_endpoint_returns_row_shape(client, alice, monkeypatch):
    """Happy-path round-trip via the HTTP poll endpoint. Asserts every
    field the dashboard's poll loop reads."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]

    def handler(session, ws_id, payload):
        return {"result": "ok", "n": payload.get("n", 0) * 2}

    monkeypatch.setitem(jobs._HANDLERS, "_test_poll_success", handler)
    job_id = _enqueue(
        workspace_id=workspace_id,
        kind="_test_poll_success",
        payload={"n": 7},
    )
    _wait_for_terminal(job_id)

    r = client.get(
        f"/workspaces/me/generation-jobs/{job_id}",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == job_id
    assert body["kind"] == "_test_poll_success"
    assert body["status"] == "success"
    assert body["result_payload"] == {"result": "ok", "n": 14}
    assert body["error"] is None
    assert body["attempt_count"] == 1
    assert body["started_at"] is not None
    assert body["finished_at"] is not None
    assert body["created_at"] is not None


def test_poll_endpoint_surfaces_handler_error(client, alice, monkeypatch):
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]

    def handler(session, ws_id, payload):
        raise ValueError("specific failure for the dashboard to display")

    monkeypatch.setitem(jobs._HANDLERS, "_test_poll_failure", handler)
    job_id = _enqueue(workspace_id=workspace_id, kind="_test_poll_failure")
    _wait_for_terminal(job_id)

    r = client.get(
        f"/workspaces/me/generation-jobs/{job_id}",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "ValueError" in body["error"]
    assert "specific failure for the dashboard" in body["error"]
    assert body["result_payload"] is None


def test_poll_endpoint_404_on_nonexistent_id(client, alice):
    api_key = alice["api_key"]["plaintext"]
    r = client.get(
        "/workspaces/me/generation-jobs/no-such-id",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "job not found"


def test_poll_endpoint_404_across_workspaces(client, alice, bob):
    """A job belonging to alice should look indistinguishable from a
    nonexistent id when read by bob — don't leak existence across
    workspaces."""
    alice_workspace_id = alice["workspace"]["id"]
    bob_key = bob["api_key"]["plaintext"]

    job_id = _enqueue(
        workspace_id=alice_workspace_id,
        kind=f"_test_cross_workspace_{uuid.uuid4().hex[:8]}",
    )

    r = client.get(
        f"/workspaces/me/generation-jobs/{job_id}",
        headers=auth_headers(bob_key),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "job not found"


def test_poll_endpoint_unauthenticated(client):
    r = client.get("/workspaces/me/generation-jobs/any-id")
    assert r.status_code == 401


# ---------- enqueue_job round-trip ---------- #


def test_enqueue_job_writes_jsonb_payload(client, alice):
    """Handler reads request_payload as a dict; column is jsonb. Confirm
    the round-trip preserves nested structure (lists, ints, bools)."""
    workspace_id = alice["workspace"]["id"]
    payload = {
        "description": "test",
        "target_agents": ["polaris", "hermes"],
        "nested": {"key": "value", "n": 42, "flag": True},
    }
    job_id = _enqueue(
        workspace_id=workspace_id,
        kind=f"_test_jsonb_{uuid.uuid4().hex[:8]}",
        payload=payload,
    )

    s = SessionLocal()
    try:
        from sqlalchemy import text
        row = s.execute(
            text("SELECT request_payload FROM generation_jobs WHERE id = :id"),
            {"id": job_id},
        ).mappings().first()
    finally:
        s.close()

    assert row is not None
    # psycopg returns jsonb columns as native Python dicts.
    assert row["request_payload"] == payload
