"""Phase 20.7: Google Drive SDK wrappers.

Mirror the 7 tools in `backend/connectors/google_drive.py`:

- list_files
- search_files
- get_file_metadata
- download_file_content
- upload_file
- create_folder
- copy_file

Two helpers in addition to the raw download/upload: base64-encoded
content is awkward to handle from bot code, so the SDK provides
`download_file_bytes(...)` / `upload_file_bytes(...)` that do the
base64 round-trip transparently. The raw `download_file_content` /
`upload_file` are still exposed for callers who already have base64
strings on hand.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from .._connectors import _invoke


CONNECTOR_TYPE = "google_drive"


def list_files(
    *,
    query: Optional[str] = None,
    page_size: int = 50,
    order_by: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """List files using Drive query syntax (e.g.
    `name contains 'budget' and trashed = false`). Returns
    `{files: [...], next_page_token}`."""
    payload: dict[str, Any] = {"page_size": page_size}
    if query:
        payload["query"] = query
    if order_by:
        payload["order_by"] = order_by
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="list_files",
        payload=payload,
        source_agent=source_agent,
    )


def search_files(
    text: str,
    *,
    page_size: int = 25,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Free-text search across file names + content. Convenience
    wrapper that builds a `name contains '...' or fullText contains
    '...' and trashed = false` query."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="search_files",
        payload={"text": text, "page_size": page_size},
        source_agent=source_agent,
    )


def get_file_metadata(
    file_id: str,
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch full metadata for a file by id."""
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="get_file_metadata",
        payload={"file_id": file_id},
        source_agent=source_agent,
    )


def download_file_content(
    file_id: str,
    *,
    export_mime_type: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Download a file's content as base64. For Google-native files
    (Docs / Sheets / Slides / Drawings) the file is auto-exported
    (text/plain, text/csv, application/pdf, image/png respectively).
    Pass `export_mime_type` to override.

    Returns `{file_id, name, source_mime_type, mime_type, size,
    content_b64}`. For raw bytes, use `download_file_bytes(...)`
    instead — it does the base64 decode automatically."""
    payload: dict[str, Any] = {"file_id": file_id}
    if export_mime_type:
        payload["export_mime_type"] = export_mime_type
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="download_file_content",
        payload=payload,
        source_agent=source_agent,
    )


def download_file_bytes(
    file_id: str,
    *,
    export_mime_type: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """Convenience wrapper around download_file_content that decodes
    the base64. Returns `(content_bytes, mime_type, name)` so bot
    code doesn't have to know about the wire encoding."""
    result = download_file_content(
        file_id,
        export_mime_type=export_mime_type,
        source_agent=source_agent,
    )
    return (
        base64.b64decode(result["content_b64"]),
        str(result.get("mime_type") or "application/octet-stream"),
        str(result.get("name") or ""),
    )


def upload_file(
    *,
    name: str,
    content_b64: str,
    mime_type: str = "application/octet-stream",
    folder_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a file. `content_b64` is base64-encoded bytes. For raw
    bytes, use `upload_file_bytes(...)` instead. v1 caps at ~5MB
    after decode (Drive's default uploadType=multipart limit)."""
    payload: dict[str, Any] = {
        "name": name,
        "content_b64": content_b64,
        "mime_type": mime_type,
    }
    if folder_id:
        payload["folder_id"] = folder_id
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="upload_file",
        payload=payload,
        source_agent=source_agent,
    )


def upload_file_bytes(
    *,
    name: str,
    content: bytes,
    mime_type: str = "application/octet-stream",
    folder_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Convenience wrapper that base64-encodes raw bytes for you."""
    return upload_file(
        name=name,
        content_b64=base64.b64encode(content).decode("ascii"),
        mime_type=mime_type,
        folder_id=folder_id,
        source_agent=source_agent,
    )


def create_folder(
    name: str,
    *,
    parent_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Create a folder. Optional `parent_id` puts it inside another
    folder; omit to put it at My Drive root."""
    payload: dict[str, Any] = {"name": name}
    if parent_id:
        payload["parent_id"] = parent_id
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="create_folder",
        payload=payload,
        source_agent=source_agent,
    )


def copy_file(
    file_id: str,
    *,
    new_name: Optional[str] = None,
    parent_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Copy a file. Omit `new_name` to keep the source name; omit
    `parent_id` to put the copy in the same folder as the source."""
    payload: dict[str, Any] = {"file_id": file_id}
    if new_name:
        payload["new_name"] = new_name
    if parent_id:
        payload["parent_id"] = parent_id
    return _invoke(
        connector_type=CONNECTOR_TYPE,
        tool_name="copy_file",
        payload=payload,
        source_agent=source_agent,
    )
