"""Phase 20.7: Google Calendar SDK wrappers.

Mirror the 7 tools in `backend/connectors/google_calendar.py`:

- list_events
- get_event
- create_event
- update_event
- delete_event
- list_calendars
- find_free_slots

calendar_id defaults to "primary" on the backend side; SDK callers
can omit it.
"""
from __future__ import annotations

from typing import Any, Optional

from .._connectors import _invoke


CONNECTOR_TYPE = "google_calendar"


def list_events(
    *,
    calendar_id: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 25,
    single_events: bool = True,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """List events in a calendar. `time_min` / `time_max` are
    RFC3339 timestamps. `single_events=True` expands recurring
    events into instances. Returns `{events: [...], next_page_token}`."""
    payload: dict[str, Any] = {
        "max_results": max_results,
        "single_events": single_events,
    }
    if calendar_id:
        payload["calendar_id"] = calendar_id
    if time_min:
        payload["time_min"] = time_min
    if time_max:
        payload["time_max"] = time_max
    if query:
        payload["query"] = query
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="list_events",
        payload=payload,
        source_agent=source_agent,
    )


def get_event(
    event_id: str,
    *,
    calendar_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch a single event by id."""
    payload: dict[str, Any] = {"event_id": event_id}
    if calendar_id:
        payload["calendar_id"] = calendar_id
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="get_event",
        payload=payload,
        source_agent=source_agent,
    )


def create_event(
    *,
    summary: str,
    start: dict[str, Any],
    end: dict[str, Any],
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[list[dict[str, Any]]] = None,
    calendar_id: Optional[str] = None,
    send_updates: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Create an event. `start` and `end` are Google-shape dicts:
    `{"dateTime": "2026-05-21T14:00:00-07:00", "timeZone": "..."}`
    for timed events, or `{"date": "2026-05-21"}` for all-day.
    `attendees` is `[{"email": "..."}]`. `send_updates` is one of
    `"all"`, `"externalOnly"`, `"none"`."""
    payload: dict[str, Any] = {
        "summary": summary,
        "start": start,
        "end": end,
    }
    if description:
        payload["description"] = description
    if location:
        payload["location"] = location
    if attendees:
        payload["attendees"] = attendees
    if calendar_id:
        payload["calendar_id"] = calendar_id
    if send_updates:
        payload["send_updates"] = send_updates
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="create_event",
        payload=payload,
        source_agent=source_agent,
    )


def update_event(
    event_id: str,
    *,
    summary: Optional[str] = None,
    start: Optional[dict[str, Any]] = None,
    end: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[list[dict[str, Any]]] = None,
    calendar_id: Optional[str] = None,
    send_updates: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Partial update — only the fields you pass land in the PATCH
    body. Backend returns 422 if you pass zero fields to change."""
    payload: dict[str, Any] = {"event_id": event_id}
    if summary is not None:
        payload["summary"] = summary
    if start is not None:
        payload["start"] = start
    if end is not None:
        payload["end"] = end
    if description is not None:
        payload["description"] = description
    if location is not None:
        payload["location"] = location
    if attendees is not None:
        payload["attendees"] = attendees
    if calendar_id:
        payload["calendar_id"] = calendar_id
    if send_updates:
        payload["send_updates"] = send_updates
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="update_event",
        payload=payload,
        source_agent=source_agent,
    )


def delete_event(
    event_id: str,
    *,
    calendar_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Delete an event. Returns `{deleted_event_id, calendar_id}`
    since the Google API's 204 has no body."""
    payload: dict[str, Any] = {"event_id": event_id}
    if calendar_id:
        payload["calendar_id"] = calendar_id
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="delete_event",
        payload=payload,
        source_agent=source_agent,
    )


def list_calendars(
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """List all calendars on the connected account."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="list_calendars",
        payload={},
        source_agent=source_agent,
    )


def find_free_slots(
    *,
    time_min: str,
    time_max: str,
    calendar_ids: Optional[list[str]] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Query Google's `/freeBusy` endpoint. Returns
    `{busy_by_calendar: {calendar_id: [{start, end}, ...]}}`. The
    caller is responsible for inverting busy → free slots within the
    `[time_min, time_max]` window. Defaults to the primary calendar
    if `calendar_ids` is omitted."""
    payload: dict[str, Any] = {
        "time_min": time_min,
        "time_max": time_max,
    }
    if calendar_ids:
        payload["calendar_ids"] = calendar_ids
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="find_free_slots",
        payload=payload,
        source_agent=source_agent,
    )
