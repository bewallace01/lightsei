"""Phase 20.7: Gmail SDK wrappers.

Typed Python functions that funnel through `_invoke` (which handles
capability check + source_agent resolution + POST + error mapping).
Mirror the 6 tools in `backend/connectors/gmail.py`:

- send_email
- search_inbox
- get_thread
- list_labels
- add_label
- mark_read

Each function returns whatever the backend's INVOKE returned — the
backend's `{ok: True, result: ...}` envelope is stripped in
`_invoke`.
"""
from __future__ import annotations

from typing import Any, Optional

from .._connectors import _invoke


CONNECTOR_TYPE = "gmail"


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Send an email from the connected Gmail account.

    Returns the Gmail API's send response: `{id, thread_id,
    label_ids}`. Raises LightseiCapabilityError if the agent doesn't
    have `connector:gmail`. Raises LightseiConnectorZoneError if the
    agent's sensitivity_level is not in Gmail's declared zones
    (public-zoned bots are refused — Gmail's declared_zones excludes
    public)."""
    payload: dict[str, Any] = {
        "to": to,
        "subject": subject,
        "body": body,
    }
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="send_email",
        payload=payload,
        source_agent=source_agent,
    )


def search_inbox(
    query: str,
    *,
    max_results: int = 20,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Search the connected inbox using Gmail query syntax (e.g.
    `is:unread`, `from:alice@example.com`, `label:^t`).

    Returns `{messages: [{id, thread_id, from, subject, snippet,
    date, label_ids}, ...]}`. max_results is clamped to [1, 100] on
    the backend."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="search_inbox",
        payload={"query": query, "max_results": max_results},
        source_agent=source_agent,
    )


def get_thread(
    thread_id: str,
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch a full Gmail thread by id (the `thread_id` returned by
    search_inbox / send_email). Returns the thread blob with all
    messages."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="get_thread",
        payload={"thread_id": thread_id},
        source_agent=source_agent,
    )


def list_labels(
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """List all labels on the connected account (system + user)."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="list_labels",
        payload={},
        source_agent=source_agent,
    )


def add_label(
    message_id: str,
    label_ids: list[str],
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Apply one or more labels to a message. `label_ids` is the
    Gmail-internal label id list (use list_labels to resolve names
    to ids)."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="add_label",
        payload={"message_id": message_id, "label_ids": label_ids},
        source_agent=source_agent,
    )


def mark_read(
    message_id: str,
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Mark a message as read (removes the UNREAD label)."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="mark_read",
        payload={"message_id": message_id},
        source_agent=source_agent,
    )
