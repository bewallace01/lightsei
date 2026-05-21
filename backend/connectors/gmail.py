"""Phase 20.3: Gmail connector implementation.

Six tools exposed via the MCP-flavored MANIFEST + dispatched by INVOKE:

- `send_email(to, subject, body, cc?, bcc?)` — RFC2822 → base64url →
  users/me/messages/send.
- `search_inbox(query, max_results?)` — Gmail-style query string;
  returns list of {id, thread_id, from, subject, snippet, date}.
- `get_thread(thread_id)` — full thread payload (all messages with
  headers + bodies).
- `list_labels()` — workspace's labels.
- `add_label(message_id, label_id)` — apply a label to a message.
- `mark_read(message_id)` — remove the UNREAD label.

INVOKE is what the Phase 20.6 bot-callable endpoint dispatches into.
Each tool function makes one or two Gmail API calls; on 401 it raises
ConnectorAuthExpired so 20.6 can refresh the access_token + retry.

Tests stub httpx so they don't hit Gmail.
"""
from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage
from typing import Any, Optional

import httpx

from . import ConnectorAuthExpired, ConnectorCallError

logger = logging.getLogger("lightsei.connectors.gmail")


GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


# ---------- MCP-flavored manifest ---------- #


def MANIFEST() -> list[dict[str, Any]]:
    """Tool definitions for this connector. Shape matches Anthropic's
    tool-use input schema so the SDK can pass these straight into a
    `client.messages.create(tools=[...])` call when bot code reads
    them via `lightsei.gmail.manifest()` (the 20.7 SDK helpers will
    eventually surface a typed wrapper too)."""
    return [
        {
            "name": "send_email",
            "description": (
                "Send an email from the connected Gmail account. To, "
                "subject, and body are required. cc and bcc are optional "
                "comma-separated lists."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient address(es), comma-separated.",
                    },
                    "subject": {"type": "string"},
                    "body": {
                        "type": "string",
                        "description": (
                            "Plain-text body. HTML emails come in a future "
                            "iteration; for now the body is sent as text/plain."
                        ),
                    },
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_inbox",
            "description": (
                "Search Gmail with a Gmail-syntax query (e.g. "
                "'is:unread label:^t', 'from:billing@acme.com newer_than:7d'). "
                "Returns up to max_results message summaries."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {
                        "type": "integer",
                        "description": "1-100; default 20.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_thread",
            "description": (
                "Fetch a full thread (all messages, with headers + bodies). "
                "thread_id comes from search_inbox results."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                },
                "required": ["thread_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_labels",
            "description": "List all labels on the connected account.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "add_label",
            "description": (
                "Apply a label to a message. label_id comes from list_labels "
                "(system labels: INBOX, UNREAD, IMPORTANT, STARRED, ...)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "label_id": {"type": "string"},
                },
                "required": ["message_id", "label_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mark_read",
            "description": "Remove the UNREAD label from a message.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                },
                "required": ["message_id"],
                "additionalProperties": False,
            },
        },
    ]


# ---------- Dispatcher ---------- #


def INVOKE(*, tool_name: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    """Phase 20.6 bot-callable endpoint dispatches into this. Returns
    the tool's response as a dict.

    Raises ConnectorAuthExpired on 401 so the caller can refresh the
    access_token and retry once. Raises ConnectorCallError on any
    other non-2xx or transport failure.
    """
    fn = _TOOLS.get(tool_name)
    if fn is None:
        raise ConnectorCallError(
            f"unknown gmail tool {tool_name!r}",
            upstream_status=None,
        )
    return fn(payload, access_token)


# ---------- Per-tool implementations ---------- #


def _send_email(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    to = payload.get("to")
    subject = payload.get("subject")
    body = payload.get("body")
    if not (to and subject and body):
        raise ConnectorCallError("send_email requires to + subject + body")

    msg = EmailMessage()
    msg["To"] = to
    if payload.get("cc"):
        msg["Cc"] = payload["cc"]
    if payload.get("bcc"):
        msg["Bcc"] = payload["bcc"]
    msg["Subject"] = subject
    msg.set_content(body)
    # Gmail wants base64url-encoded RFC2822 in the `raw` field.
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")

    response = _request(
        "POST",
        "/messages/send",
        access_token,
        json_body={"raw": raw},
    )
    return {
        "id": response.get("id"),
        "thread_id": response.get("threadId"),
        "label_ids": response.get("labelIds") or [],
    }


def _search_inbox(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    query = payload.get("query")
    if not query:
        raise ConnectorCallError("search_inbox requires query")
    max_results = max(1, min(100, int(payload.get("max_results") or 20)))

    # 1. List message IDs matching the query.
    listing = _request(
        "GET",
        "/messages",
        access_token,
        params={"q": query, "maxResults": max_results},
    )
    items = listing.get("messages") or []

    # 2. Fetch metadata headers for each. Single-request batching isn't
    # exposed cleanly via httpx; serial is fine for max_results=20.
    summaries: list[dict[str, Any]] = []
    for it in items:
        msg_id = it.get("id")
        if not msg_id:
            continue
        meta = _request(
            "GET",
            f"/messages/{msg_id}",
            access_token,
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date"],
            },
        )
        headers = {h["name"]: h["value"] for h in (meta.get("payload") or {}).get("headers") or []}
        summaries.append({
            "id": msg_id,
            "thread_id": meta.get("threadId"),
            "snippet": meta.get("snippet") or "",
            "from": headers.get("From"),
            "subject": headers.get("Subject"),
            "date": headers.get("Date"),
            "label_ids": meta.get("labelIds") or [],
        })

    return {"messages": summaries, "result_size_estimate": listing.get("resultSizeEstimate")}


def _get_thread(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    thread_id = payload.get("thread_id")
    if not thread_id:
        raise ConnectorCallError("get_thread requires thread_id")
    full = _request(
        "GET",
        f"/threads/{thread_id}",
        access_token,
        params={"format": "full"},
    )
    return {
        "id": full.get("id"),
        "history_id": full.get("historyId"),
        "messages": full.get("messages") or [],
    }


def _list_labels(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    result = _request("GET", "/labels", access_token)
    return {"labels": result.get("labels") or []}


def _add_label(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    message_id = payload.get("message_id")
    label_id = payload.get("label_id")
    if not (message_id and label_id):
        raise ConnectorCallError("add_label requires message_id + label_id")
    result = _request(
        "POST",
        f"/messages/{message_id}/modify",
        access_token,
        json_body={"addLabelIds": [label_id]},
    )
    return {
        "id": result.get("id"),
        "label_ids": result.get("labelIds") or [],
    }


def _mark_read(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    message_id = payload.get("message_id")
    if not message_id:
        raise ConnectorCallError("mark_read requires message_id")
    result = _request(
        "POST",
        f"/messages/{message_id}/modify",
        access_token,
        json_body={"removeLabelIds": ["UNREAD"]},
    )
    return {
        "id": result.get("id"),
        "label_ids": result.get("labelIds") or [],
    }


_TOOLS: dict[str, Any] = {
    "send_email": _send_email,
    "search_inbox": _search_inbox,
    "get_thread": _get_thread,
    "list_labels": _list_labels,
    "add_label": _add_label,
    "mark_read": _mark_read,
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
    """Make one Gmail API call. Raises ConnectorAuthExpired on 401 so
    the 20.6 endpoint can refresh + retry. Raises ConnectorCallError
    on any other non-2xx or transport failure."""
    url = f"{GMAIL_BASE}{path}"
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
        logger.exception("gmail: %s %s transport failed", method, path)
        raise ConnectorCallError(f"gmail transport error: {exc}") from exc

    if resp.status_code == 401:
        # Access token expired or revoked. 20.6 catches this + refreshes.
        raise ConnectorAuthExpired("gmail returned 401")

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:300]}
        logger.warning("gmail: %s %s returned %s: %s", method, path, resp.status_code, body)
        raise ConnectorCallError(
            f"gmail {method} {path} returned {resp.status_code}",
            upstream_status=resp.status_code,
        )

    try:
        return resp.json() if resp.content else {}
    except Exception as exc:
        raise ConnectorCallError("gmail returned malformed JSON") from exc
