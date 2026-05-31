"""Phase 30.3.f: SDK polls BOTH thread + team claim endpoints.

The deployed bot's @on_chat handler is identity-agnostic — it just
receives a history of messages and returns a reply. What changed in
30.3.f: _ChatPoller now polls two claim endpoints per tick (per-bot
threads + workspace-team) and routes the resulting complete/chunk
POSTs to the right URL pair so the team handler in the backend
(30.3.e) sees the response.

Tests use a hand-rolled fake HTTP client and exercise _tick_once
directly so the assertion surface is the URL set the poller hits,
not real network traffic. Heavier end-to-end tests live in the
backend (Phase 30.3.c + .e).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import lightsei
from lightsei import _chat
from lightsei._chat import _ChatPoller
from lightsei._client import _client


@pytest.fixture(autouse=True)
def _reset_chat_state():
    yield
    _client._reset_for_tests()
    _chat._handlers.clear()


class _FakeHTTP:
    """Records every POST. `responses` maps a URL substring to a
    SimpleNamespace(status_code, json_data); first matching entry
    wins. Default = 200 + {"turn": null}."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.posts: list[tuple[str, dict | None]] = []

    def post(self, url: str, *, json: dict | None = None, timeout=None):
        self.posts.append((url, json))
        for needle, resp in self.responses.items():
            if needle in url:
                return resp
        return SimpleNamespace(
            status_code=200, json=lambda: {"turn": None},
        )


def _poller_with(http, agent_name: str = "argus") -> _ChatPoller:
    _client._http = http  # type: ignore[attr-defined]
    _client.agent_name = agent_name
    _client.timeout = 5.0
    return _ChatPoller(_client, interval=0.01)


# ---------- Surface coverage ---------- #


def test_tick_calls_both_claim_endpoints(monkeypatch):
    """Every tick polls per-bot threads AND the workspace-team
    surface. The order doesn't matter; both URLs must appear."""
    http = _FakeHTTP({})  # both claims return turn=null
    p = _poller_with(http)
    p._tick_once()

    paths = [u for u, _ in http.posts]
    assert "/agents/argus/threads/claim" in paths
    assert "/agents/argus/team-conversations/claim" in paths


def test_team_claim_dispatches_through_team_urls():
    """A team turn must complete via /team-messages/{id}/complete,
    NOT /messages/{id}/complete. Wrong URL = pending row never
    finalizes + the operator sees 'thinking...' forever."""
    team_turn = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "turn": {
                "message_id": "tm-1",
                "conversation_id": "conv-1",
                "messages": [
                    {"role": "user", "content": "scan"},
                ],
            },
        },
    )
    http = _FakeHTTP({
        "/team-conversations/claim": team_turn,
        # thread claim returns empty so only the team path runs
    })

    @lightsei.on_chat
    def handler(messages):
        return "no secrets found."

    p = _poller_with(http)
    p._tick_once()

    paths = [u for u, _ in http.posts]
    assert "/team-messages/tm-1/complete" in paths
    # And NOT the thread complete URL.
    assert "/messages/tm-1/complete" not in paths

    complete_body = next(
        body for url, body in http.posts
        if url == "/team-messages/tm-1/complete"
    )
    assert complete_body == {"content": "no secrets found."}


def test_thread_claim_still_dispatches_through_thread_urls():
    """Regression: 30.3.f refactor must not break the existing
    per-bot threads path."""
    thread_turn = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "turn": {
                "message_id": "msg-1",
                "thread_id": "thr-1",
                "messages": [{"role": "user", "content": "hi"}],
            },
        },
    )
    http = _FakeHTTP({"/threads/claim": thread_turn})

    @lightsei.on_chat
    def handler(messages):
        return "hello back."

    p = _poller_with(http)
    p._tick_once()

    paths = [u for u, _ in http.posts]
    assert "/messages/msg-1/complete" in paths
    assert "/team-messages/msg-1/complete" not in paths


def test_team_streaming_uses_team_chunk_url():
    """Generator handler streams via chunk POSTs; for a team turn
    those must hit /team-messages/{id}/chunk, not /messages/.../chunk."""
    team_turn = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "turn": {
                "message_id": "tm-2",
                "conversation_id": "conv-2",
                "messages": [{"role": "user", "content": "stream"}],
            },
        },
    )
    http = _FakeHTTP({"/team-conversations/claim": team_turn})

    @lightsei.on_chat
    def handler(messages):
        yield "scanning "
        yield "files... "
        yield "done."

    p = _poller_with(http)
    p._tick_once()

    chunk_urls = [u for u, _ in http.posts if "/chunk" in u]
    assert chunk_urls == [
        "/team-messages/tm-2/chunk",
        "/team-messages/tm-2/chunk",
        "/team-messages/tm-2/chunk",
    ]
    deltas = [body["delta"] for u, body in http.posts if "/chunk" in u]
    assert deltas == ["scanning ", "files... ", "done."]
    # After streaming, a complete with no content (server keeps the
    # accumulated value) goes to the team complete URL.
    final = next(
        body for url, body in http.posts
        if url == "/team-messages/tm-2/complete"
    )
    assert final == {}


def test_team_error_uses_team_complete_url():
    team_turn = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "turn": {
                "message_id": "tm-err",
                "conversation_id": "conv-err",
                "messages": [{"role": "user", "content": "boom"}],
            },
        },
    )
    http = _FakeHTTP({"/team-conversations/claim": team_turn})

    @lightsei.on_chat
    def handler(messages):
        raise RuntimeError("scanner crashed")

    p = _poller_with(http)
    p._tick_once()

    err_body = next(
        body for url, body in http.posts
        if url == "/team-messages/tm-err/complete"
    )
    assert "error" in err_body
    assert "scanner crashed" in err_body["error"]


def test_no_handler_team_path_still_completes_with_error():
    """If the bot has no @on_chat handler, team-turn rows still get
    completed (with an error) so they don't sit pending forever."""
    team_turn = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "turn": {
                "message_id": "tm-no-h",
                "conversation_id": "c",
                "messages": [{"role": "user", "content": "x"}],
            },
        },
    )
    http = _FakeHTTP({"/team-conversations/claim": team_turn})
    # Do NOT register a handler.
    p = _poller_with(http)
    p._tick_once()

    err_body = next(
        body for url, body in http.posts
        if url == "/team-messages/tm-no-h/complete"
    )
    assert "error" in err_body
    assert "no chat handler" in err_body["error"]
