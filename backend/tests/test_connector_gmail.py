"""Phase 20.3: Gmail connector tests.

Stubs httpx.request so tests don't hit Gmail. Each tool has at least
one happy-path test + 401 raises ConnectorAuthExpired + unknown-tool
raises ConnectorCallError.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from connectors import (
    CONNECTOR_REGISTRY,
    ConnectorAuthExpired,
    ConnectorCallError,
)
from connectors import gmail as gmail_mod


def _resp(status: int, body: Any) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        content=json.dumps(body).encode() if body is not None else b"",
        text=json.dumps(body) if body is not None else "",
    )


class _HttpxCapture:
    """Stand-in for httpx.request that records calls + returns a script
    of responses in order."""

    def __init__(self, responses: list[SimpleNamespace]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs) -> SimpleNamespace:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise RuntimeError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


# ---------- MANIFEST ---------- #


def test_manifest_lists_six_tools():
    tools = gmail_mod.MANIFEST()
    names = {t["name"] for t in tools}
    assert names == {
        "send_email", "search_inbox", "get_thread",
        "list_labels", "add_label", "mark_read",
    }


def test_manifest_send_email_requires_to_subject_body():
    tools = {t["name"]: t for t in gmail_mod.MANIFEST()}
    schema = tools["send_email"]["input_schema"]
    assert set(schema["required"]) == {"to", "subject", "body"}
    assert "cc" in schema["properties"]
    assert "bcc" in schema["properties"]


def test_registry_wires_gmail_to_real_module():
    """The 20.1 stub should be gone — the registry now points at the
    real INVOKE."""
    spec = CONNECTOR_REGISTRY["gmail"]
    tools = spec.manifest()
    assert tools  # non-empty (stub was empty)
    assert {t["name"] for t in tools} >= {"send_email", "search_inbox"}


# ---------- send_email ---------- #


def test_send_email_builds_base64url_rfc2822(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"id": "MSG_1", "threadId": "TH_1", "labelIds": ["SENT"]})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="send_email",
        payload={"to": "alice@example.com", "subject": "hi", "body": "hello world"},
        access_token="ya29.fake",
    )
    assert result == {"id": "MSG_1", "thread_id": "TH_1", "label_ids": ["SENT"]}

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/messages/send")
    # Verify the base64url-encoded raw includes the recipient + subject + body.
    raw = call["json"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    assert "To: alice@example.com" in decoded
    assert "Subject: hi" in decoded
    assert "hello world" in decoded
    # Authorization header carried.
    assert call["headers"]["Authorization"] == "Bearer ya29.fake"


def test_send_email_missing_required_fields_raises():
    with pytest.raises(ConnectorCallError):
        gmail_mod.INVOKE(
            tool_name="send_email",
            payload={"to": "x@y.com", "subject": "no body"},
            access_token="t",
        )


def test_send_email_401_raises_auth_expired(monkeypatch):
    stub = _HttpxCapture([_resp(401, {"error": "invalid_token"})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    with pytest.raises(ConnectorAuthExpired):
        gmail_mod.INVOKE(
            tool_name="send_email",
            payload={"to": "a@b.com", "subject": "x", "body": "y"},
            access_token="dead-token",
        )


def test_send_email_4xx_other_raises_call_error(monkeypatch):
    """403 (e.g. insufficient scope) is not an auth-expired error —
    surfaces as ConnectorCallError so 20.6 doesn't burn a refresh
    cycle on it."""
    stub = _HttpxCapture([_resp(403, {"error": "insufficient_scope"})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    with pytest.raises(ConnectorCallError) as exc:
        gmail_mod.INVOKE(
            tool_name="send_email",
            payload={"to": "a@b.com", "subject": "x", "body": "y"},
            access_token="t",
        )
    assert exc.value.upstream_status == 403


def test_send_email_transport_failure_raises(monkeypatch):
    import httpx
    def _boom(*a, **kw):
        raise httpx.ConnectError("dns failed")
    monkeypatch.setattr("connectors.gmail.httpx.request", _boom)

    with pytest.raises(ConnectorCallError):
        gmail_mod.INVOKE(
            tool_name="send_email",
            payload={"to": "a@b.com", "subject": "x", "body": "y"},
            access_token="t",
        )


# ---------- search_inbox ---------- #


def test_search_inbox_lists_then_fetches_metadata(monkeypatch):
    """First call lists IDs; one follow-up per message fetches headers."""
    stub = _HttpxCapture([
        _resp(200, {"messages": [{"id": "M1", "threadId": "T1"}, {"id": "M2", "threadId": "T2"}], "resultSizeEstimate": 2}),
        _resp(200, {
            "threadId": "T1",
            "snippet": "Welcome",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "Subject", "value": "Welcome"},
                {"name": "Date", "value": "Tue, 20 May 2026 09:00:00 +0000"},
            ]},
        }),
        _resp(200, {
            "threadId": "T2",
            "snippet": "Hi",
            "labelIds": ["INBOX"],
            "payload": {"headers": [
                {"name": "From", "value": "Bob"},
                {"name": "Subject", "value": "Hi"},
            ]},
        }),
    ])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="search_inbox",
        payload={"query": "is:unread", "max_results": 5},
        access_token="t",
    )
    assert len(result["messages"]) == 2
    first = result["messages"][0]
    assert first["id"] == "M1"
    assert first["thread_id"] == "T1"
    assert first["from"] == "Alice <alice@example.com>"
    assert first["subject"] == "Welcome"
    assert first["snippet"] == "Welcome"
    assert "UNREAD" in first["label_ids"]
    # List call uses q + maxResults.
    list_call = stub.calls[0]
    assert list_call["params"]["q"] == "is:unread"
    assert list_call["params"]["maxResults"] == 5


def test_search_inbox_empty_results(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"resultSizeEstimate": 0})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="search_inbox",
        payload={"query": "nothing matches this"},
        access_token="t",
    )
    assert result["messages"] == []


def test_search_inbox_clamps_max_results(monkeypatch):
    """Gmail caps at 100; payload max_results=500 should clamp to 100."""
    stub = _HttpxCapture([_resp(200, {"messages": []})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)
    gmail_mod.INVOKE(
        tool_name="search_inbox",
        payload={"query": "x", "max_results": 500},
        access_token="t",
    )
    assert stub.calls[0]["params"]["maxResults"] == 100


def test_search_inbox_default_max_results_20(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"messages": []})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)
    gmail_mod.INVOKE(
        tool_name="search_inbox",
        payload={"query": "x"},
        access_token="t",
    )
    assert stub.calls[0]["params"]["maxResults"] == 20


# ---------- get_thread ---------- #


def test_get_thread_returns_full_thread(monkeypatch):
    stub = _HttpxCapture([_resp(200, {
        "id": "TH_5",
        "historyId": "12345",
        "messages": [
            {"id": "M1", "payload": {"headers": []}, "snippet": "Hi"},
            {"id": "M2", "payload": {"headers": []}, "snippet": "Re: Hi"},
        ],
    })])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="get_thread",
        payload={"thread_id": "TH_5"},
        access_token="t",
    )
    assert result["id"] == "TH_5"
    assert len(result["messages"]) == 2


# ---------- list_labels ---------- #


def test_list_labels(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_42", "name": "Customers", "type": "user"},
    ]})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="list_labels",
        payload={},
        access_token="t",
    )
    assert len(result["labels"]) == 2
    assert any(label["name"] == "Customers" for label in result["labels"])


# ---------- add_label ---------- #


def test_add_label_modifies_message(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"id": "M1", "labelIds": ["INBOX", "Label_42"]})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="add_label",
        payload={"message_id": "M1", "label_id": "Label_42"},
        access_token="t",
    )
    assert result["id"] == "M1"
    assert "Label_42" in result["label_ids"]

    call = stub.calls[0]
    assert call["url"].endswith("/messages/M1/modify")
    assert call["json"] == {"addLabelIds": ["Label_42"]}


# ---------- mark_read ---------- #


def test_mark_read_removes_unread_label(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"id": "M9", "labelIds": ["INBOX"]})])
    monkeypatch.setattr("connectors.gmail.httpx.request", stub)

    result = gmail_mod.INVOKE(
        tool_name="mark_read",
        payload={"message_id": "M9"},
        access_token="t",
    )
    assert result["id"] == "M9"
    assert "UNREAD" not in result["label_ids"]
    assert stub.calls[0]["json"] == {"removeLabelIds": ["UNREAD"]}


# ---------- Dispatcher ---------- #


def test_invoke_unknown_tool_raises():
    with pytest.raises(ConnectorCallError) as exc:
        gmail_mod.INVOKE(
            tool_name="hallucinated_tool",
            payload={},
            access_token="t",
        )
    assert "hallucinated_tool" in str(exc.value)
