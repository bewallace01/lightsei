"""Phase 20.4: Google Calendar connector implementation.

Seven tools dispatched via MANIFEST + INVOKE, same shape as Gmail
(Phase 20.3). Each tool wraps one Calendar v3 REST call (free/busy
is `freeBusy.query`, everything else is on `events` or
`calendarList`).

calendar_id defaults to `"primary"` (the connected account's main
calendar) so bot code can ignore it for the common case.

Tests stub httpx; this module never hits Google's API at test time.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from . import ConnectorAuthExpired, ConnectorCallError

logger = logging.getLogger("lightsei.connectors.google_calendar")


CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


# ---------- MCP-flavored manifest ---------- #


def MANIFEST() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_events",
            "description": (
                "List events from a calendar. `time_min` / `time_max` are "
                "RFC3339 timestamps (e.g. '2026-05-20T00:00:00Z'). Optional "
                "`query` for full-text filter."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_id": {
                        "type": "string",
                        "description": "Default 'primary'.",
                    },
                    "time_min": {"type": "string"},
                    "time_max": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {
                        "type": "integer",
                        "description": "1-250; default 25.",
                    },
                    "single_events": {
                        "type": "boolean",
                        "description": (
                            "Expand recurring events into individual instances. "
                            "Default true."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "get_event",
            "description": "Fetch a single event by id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string"},
                    "event_id": {"type": "string"},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_event",
            "description": (
                "Create an event. `start` and `end` are objects "
                "{dateTime: '2026-05-20T10:00:00-07:00'} or "
                "{date: '2026-05-20'} for all-day. Attendees is a list of "
                "{email: 'x@y.com'} objects."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "start": {"type": "object"},
                    "end": {"type": "object"},
                    "attendees": {"type": "array", "items": {"type": "object"}},
                    "send_updates": {
                        "type": "string",
                        "description": "'all' | 'externalOnly' | 'none' (default 'none').",
                    },
                },
                "required": ["summary", "start", "end"],
                "additionalProperties": False,
            },
        },
        {
            "name": "update_event",
            "description": (
                "Partial update of an event. Pass only the fields you want "
                "to change."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string"},
                    "event_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "start": {"type": "object"},
                    "end": {"type": "object"},
                    "attendees": {"type": "array", "items": {"type": "object"}},
                    "send_updates": {"type": "string"},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_event",
            "description": "Delete an event.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string"},
                    "event_id": {"type": "string"},
                    "send_updates": {"type": "string"},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_calendars",
            "description": "List calendars the connected account has access to.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "find_free_slots",
            "description": (
                "Query free/busy info for one or more calendars over a window. "
                "Returns the busy intervals; bot code can subtract them from "
                "the window to find free slots."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of calendar IDs (defaults to ['primary']).",
                    },
                    "time_min": {"type": "string"},
                    "time_max": {"type": "string"},
                },
                "required": ["time_min", "time_max"],
                "additionalProperties": False,
            },
        },
    ]


# ---------- Dispatcher ---------- #


def INVOKE(*, tool_name: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    fn = _TOOLS.get(tool_name)
    if fn is None:
        raise ConnectorCallError(f"unknown google_calendar tool {tool_name!r}")
    return fn(payload, access_token)


# ---------- Per-tool implementations ---------- #


def _list_events(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    calendar_id = payload.get("calendar_id") or "primary"
    params: dict[str, Any] = {
        "maxResults": max(1, min(250, int(payload.get("max_results") or 25))),
        "singleEvents": "true" if payload.get("single_events", True) else "false",
        "orderBy": "startTime" if payload.get("single_events", True) else "updated",
    }
    if payload.get("time_min"):
        params["timeMin"] = payload["time_min"]
    if payload.get("time_max"):
        params["timeMax"] = payload["time_max"]
    if payload.get("query"):
        params["q"] = payload["query"]

    result = _request(
        "GET",
        f"/calendars/{calendar_id}/events",
        access_token,
        params=params,
    )
    return {
        "events": result.get("items") or [],
        "next_page_token": result.get("nextPageToken"),
        "next_sync_token": result.get("nextSyncToken"),
    }


def _get_event(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    event_id = payload.get("event_id")
    if not event_id:
        raise ConnectorCallError("get_event requires event_id")
    calendar_id = payload.get("calendar_id") or "primary"
    return _request(
        "GET",
        f"/calendars/{calendar_id}/events/{event_id}",
        access_token,
    )


def _create_event(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    summary = payload.get("summary")
    start = payload.get("start")
    end = payload.get("end")
    if not (summary and start and end):
        raise ConnectorCallError("create_event requires summary + start + end")
    calendar_id = payload.get("calendar_id") or "primary"

    body: dict[str, Any] = {"summary": summary, "start": start, "end": end}
    for k in ("description", "location", "attendees"):
        if payload.get(k) is not None:
            body[k] = payload[k]

    params: dict[str, Any] = {}
    if payload.get("send_updates"):
        params["sendUpdates"] = payload["send_updates"]

    return _request(
        "POST",
        f"/calendars/{calendar_id}/events",
        access_token,
        params=params or None,
        json_body=body,
    )


def _update_event(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    event_id = payload.get("event_id")
    if not event_id:
        raise ConnectorCallError("update_event requires event_id")
    calendar_id = payload.get("calendar_id") or "primary"

    body: dict[str, Any] = {}
    for k in ("summary", "description", "location", "start", "end", "attendees"):
        if payload.get(k) is not None:
            body[k] = payload[k]
    if not body:
        raise ConnectorCallError("update_event requires at least one field to update")

    params: dict[str, Any] = {}
    if payload.get("send_updates"):
        params["sendUpdates"] = payload["send_updates"]

    return _request(
        "PATCH",
        f"/calendars/{calendar_id}/events/{event_id}",
        access_token,
        params=params or None,
        json_body=body,
    )


def _delete_event(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    event_id = payload.get("event_id")
    if not event_id:
        raise ConnectorCallError("delete_event requires event_id")
    calendar_id = payload.get("calendar_id") or "primary"

    params: dict[str, Any] = {}
    if payload.get("send_updates"):
        params["sendUpdates"] = payload["send_updates"]

    _request(
        "DELETE",
        f"/calendars/{calendar_id}/events/{event_id}",
        access_token,
        params=params or None,
    )
    # Calendar's DELETE returns 204 with no body. Echo the deleted id
    # so bot code can chain without re-supplying it.
    return {"deleted_event_id": event_id, "calendar_id": calendar_id}


def _list_calendars(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    result = _request("GET", "/users/me/calendarList", access_token)
    return {"calendars": result.get("items") or []}


def _find_free_slots(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    time_min = payload.get("time_min")
    time_max = payload.get("time_max")
    if not (time_min and time_max):
        raise ConnectorCallError("find_free_slots requires time_min + time_max")
    calendar_ids = payload.get("calendar_ids") or ["primary"]
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": cid} for cid in calendar_ids],
    }
    result = _request("POST", "/freeBusy", access_token, json_body=body)
    calendars = result.get("calendars") or {}
    return {
        "time_min": time_min,
        "time_max": time_max,
        "busy_by_calendar": {
            cid: (info.get("busy") or [])
            for cid, info in calendars.items()
        },
    }


_TOOLS: dict[str, Any] = {
    "list_events": _list_events,
    "get_event": _get_event,
    "create_event": _create_event,
    "update_event": _update_event,
    "delete_event": _delete_event,
    "list_calendars": _list_calendars,
    "find_free_slots": _find_free_slots,
}


# ---------- HTTP helper ---------- #


def _request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Mirrors gmail._request — kept separate to avoid coupling
    connector modules (each can grow its own headers / pagination
    quirks). Same exception semantics: 401 → ConnectorAuthExpired,
    other 4xx/5xx → ConnectorCallError, transport → ConnectorCallError."""
    url = f"{CALENDAR_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = httpx.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("calendar: %s %s transport failed", method, path)
        raise ConnectorCallError(f"calendar transport error: {exc}") from exc

    if resp.status_code == 401:
        raise ConnectorAuthExpired("calendar returned 401")

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:300]}
        logger.warning("calendar: %s %s returned %s: %s", method, path, resp.status_code, body)
        raise ConnectorCallError(
            f"calendar {method} {path} returned {resp.status_code}",
            upstream_status=resp.status_code,
        )

    # DELETE returns 204 with no body — handle gracefully.
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception as exc:
        raise ConnectorCallError("calendar returned malformed JSON") from exc
