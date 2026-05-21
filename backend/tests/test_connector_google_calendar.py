"""Phase 20.4: Google Calendar connector tests.

Stubs httpx.request so tests don't hit Calendar. Same shape as
test_connector_gmail.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from connectors import (
    CONNECTOR_REGISTRY,
    ConnectorAuthExpired,
    ConnectorCallError,
)
from connectors import google_calendar as cal_mod


def _resp(status: int, body: Any) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        content=json.dumps(body).encode() if body is not None else b"",
        text=json.dumps(body) if body is not None else "",
    )


class _HttpxCapture:
    def __init__(self, responses: list[SimpleNamespace]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs) -> SimpleNamespace:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise RuntimeError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


# ---------- MANIFEST ---------- #


def test_manifest_lists_seven_tools():
    tools = cal_mod.MANIFEST()
    names = {t["name"] for t in tools}
    assert names == {
        "list_events", "get_event", "create_event", "update_event",
        "delete_event", "list_calendars", "find_free_slots",
    }


def test_registry_wires_calendar_to_real_module():
    spec = CONNECTOR_REGISTRY["google_calendar"]
    tools = spec.manifest()
    assert tools  # non-empty (stub gone)
    assert {t["name"] for t in tools} >= {"list_events", "create_event"}


# ---------- list_events ---------- #


def test_list_events_defaults_to_primary_calendar(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"items": [], "nextPageToken": None})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)

    cal_mod.INVOKE(
        tool_name="list_events",
        payload={"time_min": "2026-05-20T00:00:00Z"},
        access_token="t",
    )
    call = stub.calls[0]
    assert "/calendars/primary/events" in call["url"]
    assert call["params"]["timeMin"] == "2026-05-20T00:00:00Z"
    assert call["params"]["singleEvents"] == "true"
    assert call["params"]["maxResults"] == 25


def test_list_events_passes_query_and_clamps_max_results(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"items": [{"id": "e1"}]})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)

    result = cal_mod.INVOKE(
        tool_name="list_events",
        payload={
            "time_min": "2026-05-20T00:00:00Z",
            "time_max": "2026-05-27T00:00:00Z",
            "query": "standup",
            "max_results": 999,  # clamp to 250
        },
        access_token="t",
    )
    assert result["events"] == [{"id": "e1"}]
    call = stub.calls[0]
    assert call["params"]["q"] == "standup"
    assert call["params"]["maxResults"] == 250


def test_list_events_401_raises_auth_expired(monkeypatch):
    stub = _HttpxCapture([_resp(401, {"error": "invalid_token"})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)
    with pytest.raises(ConnectorAuthExpired):
        cal_mod.INVOKE(
            tool_name="list_events",
            payload={},
            access_token="dead",
        )


# ---------- get_event ---------- #


def test_get_event_requires_event_id():
    with pytest.raises(ConnectorCallError):
        cal_mod.INVOKE(tool_name="get_event", payload={}, access_token="t")


def test_get_event_returns_body(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"id": "EV_42", "summary": "Standup"})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)
    result = cal_mod.INVOKE(
        tool_name="get_event",
        payload={"event_id": "EV_42"},
        access_token="t",
    )
    assert result == {"id": "EV_42", "summary": "Standup"}
    assert "/calendars/primary/events/EV_42" in stub.calls[0]["url"]


# ---------- create_event ---------- #


def test_create_event_posts_full_body(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"id": "NEW_1", "summary": "Customer call"})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)

    result = cal_mod.INVOKE(
        tool_name="create_event",
        payload={
            "summary": "Customer call",
            "start": {"dateTime": "2026-05-20T15:00:00-07:00"},
            "end": {"dateTime": "2026-05-20T15:30:00-07:00"},
            "description": "weekly check-in",
            "attendees": [{"email": "alice@example.com"}],
            "send_updates": "all",
        },
        access_token="t",
    )
    assert result["id"] == "NEW_1"
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["json"]["summary"] == "Customer call"
    assert call["json"]["start"]["dateTime"].startswith("2026-05-20")
    assert call["json"]["attendees"] == [{"email": "alice@example.com"}]
    assert call["params"]["sendUpdates"] == "all"


def test_create_event_requires_summary_start_end():
    with pytest.raises(ConnectorCallError) as exc:
        cal_mod.INVOKE(
            tool_name="create_event",
            payload={"summary": "no times"},
            access_token="t",
        )
    assert "summary + start + end" in str(exc.value)


# ---------- update_event ---------- #


def test_update_event_patches_only_provided_fields(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"id": "EV_1", "summary": "Renamed"})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)

    cal_mod.INVOKE(
        tool_name="update_event",
        payload={"event_id": "EV_1", "summary": "Renamed"},
        access_token="t",
    )
    call = stub.calls[0]
    assert call["method"] == "PATCH"
    assert call["json"] == {"summary": "Renamed"}


def test_update_event_requires_at_least_one_field(monkeypatch):
    with pytest.raises(ConnectorCallError) as exc:
        cal_mod.INVOKE(
            tool_name="update_event",
            payload={"event_id": "EV_X"},
            access_token="t",
        )
    assert "at least one field" in str(exc.value)


# ---------- delete_event ---------- #


def test_delete_event_returns_deleted_id(monkeypatch):
    stub = _HttpxCapture([_resp(204, None)])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)

    result = cal_mod.INVOKE(
        tool_name="delete_event",
        payload={"event_id": "EV_42", "send_updates": "all"},
        access_token="t",
    )
    assert result == {"deleted_event_id": "EV_42", "calendar_id": "primary"}
    call = stub.calls[0]
    assert call["method"] == "DELETE"
    assert call["params"]["sendUpdates"] == "all"


# ---------- list_calendars ---------- #


def test_list_calendars(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"items": [
        {"id": "primary", "summary": "ops@example.com"},
        {"id": "team@example.com", "summary": "Team"},
    ]})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)
    result = cal_mod.INVOKE(
        tool_name="list_calendars",
        payload={},
        access_token="t",
    )
    assert len(result["calendars"]) == 2


# ---------- find_free_slots ---------- #


def test_find_free_slots_posts_freebusy(monkeypatch):
    stub = _HttpxCapture([_resp(200, {
        "calendars": {
            "primary": {"busy": [
                {"start": "2026-05-20T15:00:00Z", "end": "2026-05-20T16:00:00Z"},
            ]},
        },
    })])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)

    result = cal_mod.INVOKE(
        tool_name="find_free_slots",
        payload={
            "time_min": "2026-05-20T00:00:00Z",
            "time_max": "2026-05-21T00:00:00Z",
        },
        access_token="t",
    )
    assert "primary" in result["busy_by_calendar"]
    assert len(result["busy_by_calendar"]["primary"]) == 1
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/freeBusy")
    assert call["json"]["items"] == [{"id": "primary"}]


def test_find_free_slots_multi_calendar(monkeypatch):
    stub = _HttpxCapture([_resp(200, {"calendars": {"a": {"busy": []}, "b": {"busy": []}}})])
    monkeypatch.setattr("connectors.google_calendar.httpx.request", stub)
    result = cal_mod.INVOKE(
        tool_name="find_free_slots",
        payload={
            "time_min": "t0",
            "time_max": "t1",
            "calendar_ids": ["a", "b"],
        },
        access_token="t",
    )
    assert set(result["busy_by_calendar"].keys()) == {"a", "b"}
    assert stub.calls[0]["json"]["items"] == [{"id": "a"}, {"id": "b"}]


def test_find_free_slots_requires_time_window():
    with pytest.raises(ConnectorCallError):
        cal_mod.INVOKE(
            tool_name="find_free_slots",
            payload={},
            access_token="t",
        )


# ---------- Dispatcher ---------- #


def test_invoke_unknown_tool_raises():
    with pytest.raises(ConnectorCallError) as exc:
        cal_mod.INVOKE(tool_name="bogus_tool", payload={}, access_token="t")
    assert "bogus_tool" in str(exc.value)
