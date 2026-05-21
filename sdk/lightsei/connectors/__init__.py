"""Phase 20.7: Lightsei connector SDK.

Per-connector submodules expose typed wrappers around the
bot-callable backend endpoint shipped in Phase 20.6
(POST /connectors/{type}/{tool}). Bot code reads cleanly:

    upcoming = lightsei.calendar.list_events(time_min="now", days=7)
    unread = lightsei.gmail.search_inbox("is:unread", limit=10)
    files = lightsei.drive.list_files(query="modifiedTime > '2026-05-01'")

Each function checks the agent's local capability cache before
making the HTTP call, then maps the backend's typed error shapes
(403 capability_missing, 403 connector_zone_mismatch, 502
connector_call_failed, etc.) to the SDK's typed exception classes
in `lightsei.errors`.

Re-exported at the package root as `lightsei.gmail`,
`lightsei.calendar`, `lightsei.drive` for the canonical bot-code
ergonomics.
"""
from . import gmail, google_calendar, google_drive

__all__ = ["gmail", "google_calendar", "google_drive"]
