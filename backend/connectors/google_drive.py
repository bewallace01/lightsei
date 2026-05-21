"""Phase 20.5: Google Drive connector implementation.

Seven tools dispatched via MANIFEST + INVOKE, same shape as Gmail
(20.3) and Calendar (20.4). Notable per-tool behaviors:

- `download_file_content` returns the file as base64-encoded
  `content_b64` (JSON-safe over the bot-callable endpoint). Google-
  native files (Docs, Sheets, Slides) auto-export to text/plain,
  text/csv, application/pdf respectively — bot code doesn't have to
  know which file type it grabbed.

- `upload_file` accepts `content_b64` (base64-encoded bytes) and
  uses Drive's `uploadType=multipart` so metadata + content travel
  in one request. Resumable upload for large files is a follow-up;
  v1 caps at the default Google multipart limit (~5MB).
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

import httpx

from . import ConnectorAuthExpired, ConnectorCallError

logger = logging.getLogger("lightsei.connectors.google_drive")


DRIVE_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

# Google-native mime types that need /export instead of ?alt=media.
# Maps the Google type → the sensible export format. Bot code usually
# wants text out of Docs, csv out of Sheets, PDF out of Slides — these
# are the common-case answers. Pass `export_mime_type` to override.
_GOOGLE_NATIVE_EXPORT_DEFAULTS: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing": "image/png",
}


# ---------- MCP-flavored manifest ---------- #


def MANIFEST() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_files",
            "description": (
                "List files in Drive, optionally filtered by Drive-syntax "
                "query (e.g. \"name contains 'budget' and trashed=false\")."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "page_size": {
                        "type": "integer",
                        "description": "1-1000; default 50.",
                    },
                    "order_by": {
                        "type": "string",
                        "description": (
                            "Default 'modifiedTime desc' (newest first). "
                            "Other options: 'createdTime', 'name', 'starred'."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "search_files",
            "description": (
                "Search files by free-text. Convenience wrapper around "
                "list_files with a name+fullText OR query."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "page_size": {"type": "integer"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_file_metadata",
            "description": "Fetch the full metadata blob for a file by id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                },
                "required": ["file_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "download_file_content",
            "description": (
                "Download a file's content as base64. For Google-native files "
                "(Docs / Sheets / Slides) the file is auto-exported "
                "(Docs → text/plain, Sheets → text/csv, Slides → PDF). Pass "
                "explicit `export_mime_type` to override."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "export_mime_type": {
                        "type": "string",
                        "description": (
                            "Override the default export mime type. Only "
                            "used for Google-native files; ignored for "
                            "regular files."
                        ),
                    },
                },
                "required": ["file_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "upload_file",
            "description": (
                "Upload a file. `content_b64` is base64-encoded bytes; "
                "`mime_type` is the file's content-type (e.g. 'text/plain', "
                "'application/pdf'). Optional `folder_id` puts the file in "
                "a specific folder. v1 caps at ~5MB after decode."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content_b64": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "folder_id": {"type": "string"},
                },
                "required": ["name", "content_b64"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_folder",
            "description": "Create a folder. Optional parent folder.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "parent_id": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "copy_file",
            "description": "Make a copy of a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "new_name": {"type": "string"},
                    "parent_id": {"type": "string"},
                },
                "required": ["file_id"],
                "additionalProperties": False,
            },
        },
    ]


# ---------- Dispatcher ---------- #


def INVOKE(*, tool_name: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    fn = _TOOLS.get(tool_name)
    if fn is None:
        raise ConnectorCallError(f"unknown google_drive tool {tool_name!r}")
    return fn(payload, access_token)


# ---------- Per-tool implementations ---------- #


# Fields returned for list/search/get. Bot code usually wants id +
# name + mime type + the timestamps + (for files) size. Drive's
# `fields=*` would also work but pulls a lot of extra junk.
_DEFAULT_FILE_FIELDS = "files(id,name,mimeType,modifiedTime,createdTime,size,iconLink,webViewLink,parents,trashed,owners),nextPageToken"


def _list_files(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "pageSize": max(1, min(1000, int(payload.get("page_size") or 50))),
        "fields": _DEFAULT_FILE_FIELDS,
        "orderBy": payload.get("order_by") or "modifiedTime desc",
    }
    if payload.get("query"):
        params["q"] = payload["query"]

    result = _request("GET", "/files", access_token, params=params)
    return {
        "files": result.get("files") or [],
        "next_page_token": result.get("nextPageToken"),
    }


def _search_files(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    text = payload.get("text")
    if not text:
        raise ConnectorCallError("search_files requires text")
    # Drive query escaping: any ' in the text becomes \'.
    escaped = text.replace("'", "\\'")
    q = f"(name contains '{escaped}' or fullText contains '{escaped}') and trashed = false"
    return _list_files(
        {
            "query": q,
            "page_size": payload.get("page_size") or 25,
        },
        access_token,
    )


def _get_file_metadata(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    file_id = payload.get("file_id")
    if not file_id:
        raise ConnectorCallError("get_file_metadata requires file_id")
    return _request(
        "GET",
        f"/files/{file_id}",
        access_token,
        params={"fields": "id,name,mimeType,modifiedTime,createdTime,size,iconLink,webViewLink,parents,trashed,owners,description"},
    )


def _download_file_content(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    file_id = payload.get("file_id")
    if not file_id:
        raise ConnectorCallError("download_file_content requires file_id")

    # Fetch metadata first so we can decide between ?alt=media (regular
    # files) and /export?mimeType=... (Google-native files). One extra
    # call per download is fine; bot code can call get_file_metadata
    # ahead of time and pass `export_mime_type` to skip this if it
    # already knows the file type.
    meta = _request(
        "GET",
        f"/files/{file_id}",
        access_token,
        params={"fields": "id,name,mimeType,size"},
    )
    src_mime = meta.get("mimeType") or "application/octet-stream"
    name = meta.get("name") or "untitled"

    if src_mime in _GOOGLE_NATIVE_EXPORT_DEFAULTS:
        export_mime = payload.get("export_mime_type") or _GOOGLE_NATIVE_EXPORT_DEFAULTS[src_mime]
        path = f"/files/{file_id}/export"
        params: dict[str, Any] = {"mimeType": export_mime}
        effective_mime = export_mime
    else:
        path = f"/files/{file_id}"
        params = {"alt": "media"}
        effective_mime = src_mime

    raw_bytes = _request_bytes(
        "GET", path, access_token, params=params,
    )
    return {
        "file_id": file_id,
        "name": name,
        "source_mime_type": src_mime,
        "mime_type": effective_mime,
        "size": len(raw_bytes),
        "content_b64": base64.b64encode(raw_bytes).decode("ascii"),
    }


def _upload_file(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    name = payload.get("name")
    content_b64 = payload.get("content_b64")
    if not (name and content_b64):
        raise ConnectorCallError("upload_file requires name + content_b64")
    mime_type = payload.get("mime_type") or "application/octet-stream"
    folder_id = payload.get("folder_id")

    try:
        content_bytes = base64.b64decode(content_b64)
    except Exception as exc:
        raise ConnectorCallError("upload_file content_b64 is not valid base64") from exc

    metadata: dict[str, Any] = {"name": name, "mimeType": mime_type}
    if folder_id:
        metadata["parents"] = [folder_id]

    # multipart/related — Drive's required shape for one-request
    # metadata + content uploads. httpx's `files=` kwarg uses
    # multipart/form-data, which is the wrong content-type, so we
    # construct the body manually.
    boundary = "lightsei_drive_upload_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + content_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"{DRIVE_UPLOAD_BASE}/files?uploadType=multipart"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": f"multipart/related; boundary={boundary}",
    }
    try:
        resp = httpx.request(
            "POST", url, headers=headers, content=body, timeout=30.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("drive: upload transport failed")
        raise ConnectorCallError(f"drive transport error: {exc}") from exc

    if resp.status_code == 401:
        raise ConnectorAuthExpired("drive upload returned 401")
    if resp.status_code >= 400:
        try:
            err_body = resp.json()
        except Exception:
            err_body = {"_raw": resp.text[:300]}
        logger.warning("drive: upload returned %s: %s", resp.status_code, err_body)
        raise ConnectorCallError(
            f"drive upload returned {resp.status_code}",
            upstream_status=resp.status_code,
        )

    try:
        return resp.json()
    except Exception as exc:
        raise ConnectorCallError("drive upload returned malformed JSON") from exc


def _create_folder(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    name = payload.get("name")
    if not name:
        raise ConnectorCallError("create_folder requires name")
    body: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if payload.get("parent_id"):
        body["parents"] = [payload["parent_id"]]
    return _request("POST", "/files", access_token, json_body=body)


def _copy_file(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    file_id = payload.get("file_id")
    if not file_id:
        raise ConnectorCallError("copy_file requires file_id")
    body: dict[str, Any] = {}
    if payload.get("new_name"):
        body["name"] = payload["new_name"]
    if payload.get("parent_id"):
        body["parents"] = [payload["parent_id"]]
    return _request(
        "POST",
        f"/files/{file_id}/copy",
        access_token,
        json_body=body or None,
    )


_TOOLS: dict[str, Any] = {
    "list_files": _list_files,
    "search_files": _search_files,
    "get_file_metadata": _get_file_metadata,
    "download_file_content": _download_file_content,
    "upload_file": _upload_file,
    "create_folder": _create_folder,
    "copy_file": _copy_file,
}


# ---------- HTTP helpers ---------- #


def _request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """JSON-returning Drive API call. Same exception semantics as
    gmail._request + calendar._request."""
    url = f"{DRIVE_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = httpx.request(
            method, url, headers=headers, params=params,
            json=json_body, timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("drive: %s %s transport failed", method, path)
        raise ConnectorCallError(f"drive transport error: {exc}") from exc

    if resp.status_code == 401:
        raise ConnectorAuthExpired("drive returned 401")

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:300]}
        logger.warning("drive: %s %s returned %s: %s", method, path, resp.status_code, body)
        raise ConnectorCallError(
            f"drive {method} {path} returned {resp.status_code}",
            upstream_status=resp.status_code,
        )

    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception as exc:
        raise ConnectorCallError("drive returned malformed JSON") from exc


def _request_bytes(
    method: str,
    path: str,
    access_token: str,
    *,
    params: Optional[dict[str, Any]] = None,
) -> bytes:
    """Bytes-returning Drive API call for downloads. Same exception
    semantics, but yields raw bytes rather than parsing as JSON."""
    url = f"{DRIVE_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = httpx.request(
            method, url, headers=headers, params=params, timeout=60.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("drive: %s %s download transport failed", method, path)
        raise ConnectorCallError(f"drive transport error: {exc}") from exc

    if resp.status_code == 401:
        raise ConnectorAuthExpired("drive download returned 401")
    if resp.status_code >= 400:
        logger.warning("drive: download %s returned %s", path, resp.status_code)
        raise ConnectorCallError(
            f"drive download {path} returned {resp.status_code}",
            upstream_status=resp.status_code,
        )
    return resp.content
