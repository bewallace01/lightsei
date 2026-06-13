"""Phase 34.1: 'ask your business team' tests.

Pure-ish module functions (enqueue + poll) against the DB, plus the
endpoint round-trip. The BI assistant isn't running in tests, so we
simulate its reply by inserting the bi.summary / bi.crash event it would
emit and assert the poller picks it up.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

import ask
from db import session_scope
from models import Agent, Event, Workspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(id=ws_id, name=f"ask-{ws_id[:8]}", created_at=_now()))
    s.flush()
    return ws_id


def _add_bi_event(s, ws: str, *, kind: str, command_id: str, **payload) -> None:
    s.add(Event(
        workspace_id=ws, run_id=str(uuid.uuid4()), agent_name="bi",
        kind=kind, payload={"command_id": command_id, **payload},
        timestamp=_now(),
    ))


# ---------- module functions ---------- #


def test_enqueue_question_creates_bi_command():
    with session_scope() as s:
        ws = _make_workspace(s)
        cmd_id = ask.enqueue_question(s, ws, "How did sales go?", _now())

    with session_scope() as s:
        row = s.execute(
            text("SELECT agent_name, kind, payload FROM commands WHERE id = :id"),
            {"id": cmd_id},
        ).mappings().first()
        assert row["agent_name"] == "bi"
        assert row["kind"] == "bi.summarize"
        assert row["payload"]["source"] == "ask"
        assert row["payload"]["question"] == "How did sales go?"
        assert "data" in row["payload"]  # recent-activity rollup attached


def test_get_answer_pending_then_answered():
    with session_scope() as s:
        ws = _make_workspace(s)
        cmd_id = ask.enqueue_question(s, ws, "q", _now())

    with session_scope() as s:
        assert ask.get_answer(s, ws, cmd_id)["status"] == "pending"

    # Simulate the BI assistant replying.
    with session_scope() as s:
        _add_bi_event(s, ws, kind="bi.summary", command_id=cmd_id,
                      summary="Sales were up 12%.")

    with session_scope() as s:
        res = ask.get_answer(s, ws, cmd_id)
        assert res["status"] == "answered"
        assert res["answer"] == "Sales were up 12%."


def test_get_answer_failed_on_crash():
    with session_scope() as s:
        ws = _make_workspace(s)
        cmd_id = ask.enqueue_question(s, ws, "q", _now())
        _add_bi_event(s, ws, kind="bi.crash", command_id=cmd_id,
                      error="no ANTHROPIC_API_KEY")

    with session_scope() as s:
        res = ask.get_answer(s, ws, cmd_id)
        assert res["status"] == "failed"
        assert "ANTHROPIC_API_KEY" in res["error"]


def test_answer_is_workspace_scoped():
    # A bi.summary in workspace A must not answer workspace B's command id.
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        cmd_id = ask.enqueue_question(s, ws_b, "q", _now())
        _add_bi_event(s, ws_a, kind="bi.summary", command_id=cmd_id,
                      summary="leaked")

    with session_scope() as s:
        assert ask.get_answer(s, ws_b, cmd_id)["status"] == "pending"


def test_bi_deployed_reflects_agent_row():
    with session_scope() as s:
        ws = _make_workspace(s)
        assert ask.bi_deployed(s, ws) is False
        s.add(Agent(workspace_id=ws, name="bi", role="executor",
                    created_at=_now(), updated_at=_now()))
        s.flush()
        assert ask.bi_deployed(s, ws) is True


# ---------- endpoints ---------- #


def test_ask_endpoint_round_trip(client, alice):
    h = auth_headers(alice["session_token"])
    ws_id = alice["workspace"]["id"]

    r = client.post("/workspaces/me/ask", headers=h,
                    json={"question": "How are we doing?"})
    assert r.status_code == 200, r.text
    cmd_id = r.json()["command_id"]
    assert r.json()["bi_assistant_deployed"] is False  # not deployed in test

    # Pending until the assistant replies.
    poll = client.get(f"/workspaces/me/ask/{cmd_id}", headers=h).json()
    assert poll["status"] == "pending"
    assert poll["question"] == "How are we doing?"

    # Simulate the reply, then the poll resolves.
    with session_scope() as s:
        _add_bi_event(s, ws_id, kind="bi.summary", command_id=cmd_id,
                      summary="Steady week.")
    poll = client.get(f"/workspaces/me/ask/{cmd_id}", headers=h).json()
    assert poll["status"] == "answered"
    assert poll["answer"] == "Steady week."


def test_ask_empty_question_400(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post("/workspaces/me/ask", headers=h, json={"question": "   "})
    assert r.status_code == 400


def test_ask_unknown_command_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get(f"/workspaces/me/ask/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404


# ---------- ask history ---------- #


def test_list_recent_asks_newest_first_with_status():
    with session_scope() as s:
        ws = _make_workspace(s)
        # Three questions; answer the first, crash the second, leave third.
        c1 = ask.enqueue_question(s, ws, "first", _now())
        c2 = ask.enqueue_question(s, ws, "second", _now())
        c3 = ask.enqueue_question(s, ws, "third", _now())
        _add_bi_event(s, ws, kind="bi.summary", command_id=c1, summary="A1")
        _add_bi_event(s, ws, kind="bi.crash", command_id=c2, error="boom")

    with session_scope() as s:
        asks = ask.list_recent_asks(s, ws)
    # Newest first: third, second, first.
    assert [a["question"] for a in asks] == ["third", "second", "first"]
    by_q = {a["question"]: a for a in asks}
    assert by_q["first"]["status"] == "answered" and by_q["first"]["answer"] == "A1"
    assert by_q["second"]["status"] == "failed"
    assert by_q["third"]["status"] == "pending"


def test_list_recent_asks_empty():
    with session_scope() as s:
        ws = _make_workspace(s)
        assert ask.list_recent_asks(s, ws) == []


def test_ask_history_endpoint(client, alice):
    h = auth_headers(alice["session_token"])
    ws_id = alice["workspace"]["id"]
    r = client.post("/workspaces/me/ask", headers=h, json={"question": "hello?"})
    cmd_id = r.json()["command_id"]
    with session_scope() as s:
        _add_bi_event(s, ws_id, kind="bi.summary", command_id=cmd_id,
                      summary="Hi there.")

    asks = client.get("/workspaces/me/ask", headers=h).json()["asks"]
    assert asks[0]["question"] == "hello?"
    assert asks[0]["status"] == "answered"
    assert asks[0]["answer"] == "Hi there."
