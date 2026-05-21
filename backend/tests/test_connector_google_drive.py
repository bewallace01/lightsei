"""Phase 20.5: Google Drive connector tests.

Stubs httpx.request so tests don't hit Drive. Same shape as
test_connector_gmail.py + test_connector_google_calendar.py.
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
from connectors import google_drive as drv_mod


def _resp_json(status: int, body: Any) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        content=json.dumps(body).encode() if body is not None else b"",
        text=json.dumps(body) if body is not None else "",
    )


def _resp_bytes(status: int, raw: bytes) -> SimpleNamespace:
    """Bytes-returning response (for download endpoint)."""
    return SimpleNamespace(
        status_code=status,
        content=raw,
        text=raw.decode("utf-8", errors="replace"),
        json=lambda: {"_raw": "bytes"},
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


# ---------- MANIFEST + registry ---------- #


def test_manifest_lists_seven_tools():
    names = {t["name"] for t in drv_mod.MANIFEST()}
    assert names == {
        "list_files", "search_files", "get_file_metadata",
        "download_file_content", "upload_file", "create_folder",
        "copy_file",
    }


def test_registry_wires_drive_to_real_module():
    spec = CONNECTOR_REGISTRY["google_drive"]
    tools = spec.manifest()
    assert tools  # non-empty (stub gone)
    assert {t["name"] for t in tools} >= {"list_files", "upload_file"}


# ---------- list_files ---------- #


def test_list_files_defaults(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {"files": [{"id": "F1", "name": "a"}]})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    result = drv_mod.INVOKE(
        tool_name="list_files",
        payload={},
        access_token="t",
    )
    assert len(result["files"]) == 1
    call = stub.calls[0]
    assert call["url"].endswith("/files")
    assert call["params"]["pageSize"] == 50
    assert call["params"]["orderBy"] == "modifiedTime desc"


def test_list_files_clamps_page_size(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {"files": []})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    drv_mod.INVOKE(
        tool_name="list_files",
        payload={"page_size": 5000},
        access_token="t",
    )
    assert stub.calls[0]["params"]["pageSize"] == 1000


def test_list_files_passes_query(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {"files": []})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    drv_mod.INVOKE(
        tool_name="list_files",
        payload={"query": "trashed = false and name contains 'budget'"},
        access_token="t",
    )
    assert "name contains 'budget'" in stub.calls[0]["params"]["q"]


# ---------- search_files ---------- #


def test_search_files_builds_drive_query(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {"files": []})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    drv_mod.INVOKE(
        tool_name="search_files",
        payload={"text": "budget"},
        access_token="t",
    )
    q = stub.calls[0]["params"]["q"]
    assert "name contains 'budget'" in q
    assert "fullText contains 'budget'" in q
    assert "trashed = false" in q


def test_search_files_escapes_single_quotes(monkeypatch):
    """A user-supplied search term with apostrophes mustn't break the
    Drive query syntax (which uses ' as a literal delimiter)."""
    stub = _HttpxCapture([_resp_json(200, {"files": []})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    drv_mod.INVOKE(
        tool_name="search_files",
        payload={"text": "O'Brien's notes"},
        access_token="t",
    )
    q = stub.calls[0]["params"]["q"]
    # Each ' in the input becomes \'.
    assert "O\\'Brien" in q


def test_search_files_requires_text():
    with pytest.raises(ConnectorCallError):
        drv_mod.INVOKE(tool_name="search_files", payload={}, access_token="t")


# ---------- get_file_metadata ---------- #


def test_get_file_metadata_returns_body(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {
        "id": "F_42", "name": "Notes", "mimeType": "text/plain",
    })])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    result = drv_mod.INVOKE(
        tool_name="get_file_metadata",
        payload={"file_id": "F_42"},
        access_token="t",
    )
    assert result["id"] == "F_42"
    assert stub.calls[0]["url"].endswith("/files/F_42")


def test_get_file_metadata_requires_file_id():
    with pytest.raises(ConnectorCallError):
        drv_mod.INVOKE(tool_name="get_file_metadata", payload={}, access_token="t")


# ---------- download_file_content ---------- #


def test_download_regular_file_uses_alt_media(monkeypatch):
    """Non-Google-native file → metadata fetch + alt=media download."""
    stub = _HttpxCapture([
        _resp_json(200, {"id": "F_X", "name": "report.pdf", "mimeType": "application/pdf", "size": "1024"}),
        _resp_bytes(200, b"PDF-DATA"),
    ])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    result = drv_mod.INVOKE(
        tool_name="download_file_content",
        payload={"file_id": "F_X"},
        access_token="t",
    )
    assert result["name"] == "report.pdf"
    assert result["source_mime_type"] == "application/pdf"
    assert result["mime_type"] == "application/pdf"
    assert result["size"] == 8
    assert base64.b64decode(result["content_b64"]) == b"PDF-DATA"
    assert stub.calls[1]["params"]["alt"] == "media"


def test_download_google_doc_exports_text(monkeypatch):
    """Google-native Doc → /export with text/plain default."""
    stub = _HttpxCapture([
        _resp_json(200, {"id": "D1", "name": "Specs", "mimeType": "application/vnd.google-apps.document"}),
        _resp_bytes(200, b"Hello from the doc"),
    ])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    result = drv_mod.INVOKE(
        tool_name="download_file_content",
        payload={"file_id": "D1"},
        access_token="t",
    )
    assert result["mime_type"] == "text/plain"
    assert result["source_mime_type"] == "application/vnd.google-apps.document"
    assert base64.b64decode(result["content_b64"]) == b"Hello from the doc"
    assert stub.calls[1]["url"].endswith("/files/D1/export")
    assert stub.calls[1]["params"]["mimeType"] == "text/plain"


def test_download_google_sheet_exports_csv(monkeypatch):
    stub = _HttpxCapture([
        _resp_json(200, {"id": "S1", "name": "Sales", "mimeType": "application/vnd.google-apps.spreadsheet"}),
        _resp_bytes(200, b"a,b,c\n1,2,3\n"),
    ])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    result = drv_mod.INVOKE(
        tool_name="download_file_content",
        payload={"file_id": "S1"},
        access_token="t",
    )
    assert result["mime_type"] == "text/csv"
    assert stub.calls[1]["params"]["mimeType"] == "text/csv"


def test_download_explicit_export_override(monkeypatch):
    """`export_mime_type` payload overrides the default."""
    stub = _HttpxCapture([
        _resp_json(200, {"id": "D1", "name": "Doc", "mimeType": "application/vnd.google-apps.document"}),
        _resp_bytes(200, b"<html>...</html>"),
    ])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    result = drv_mod.INVOKE(
        tool_name="download_file_content",
        payload={"file_id": "D1", "export_mime_type": "text/html"},
        access_token="t",
    )
    assert result["mime_type"] == "text/html"
    assert stub.calls[1]["params"]["mimeType"] == "text/html"


def test_download_401_raises_auth_expired(monkeypatch):
    stub = _HttpxCapture([
        _resp_json(200, {"id": "F", "name": "f", "mimeType": "text/plain"}),
        _resp_bytes(401, b""),
    ])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    with pytest.raises(ConnectorAuthExpired):
        drv_mod.INVOKE(
            tool_name="download_file_content",
            payload={"file_id": "F"},
            access_token="t",
        )


# ---------- upload_file ---------- #


def test_upload_file_posts_multipart_related(monkeypatch):
    """upload_file must build a multipart/related body manually
    (httpx's `files=` does multipart/form-data, which is the wrong
    content-type for Drive)."""
    stub = _HttpxCapture([_resp_json(200, {"id": "NEW_F", "name": "doc.txt"})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    content = b"hello world"
    result = drv_mod.INVOKE(
        tool_name="upload_file",
        payload={
            "name": "doc.txt",
            "content_b64": base64.b64encode(content).decode("ascii"),
            "mime_type": "text/plain",
            "folder_id": "FOLDER_1",
        },
        access_token="t",
    )
    assert result["id"] == "NEW_F"

    call = stub.calls[0]
    assert call["method"] == "POST"
    assert "uploadType=multipart" in call["url"]
    # Headers carry the multipart/related content-type with a boundary.
    assert call["headers"]["Content-Type"].startswith("multipart/related; boundary=")
    # Body carries both the metadata JSON + the raw content.
    body = call["content"]
    assert b'"name": "doc.txt"' in body
    assert b'"mimeType": "text/plain"' in body
    assert b'"parents": ["FOLDER_1"]' in body
    assert b"hello world" in body


def test_upload_file_invalid_base64_raises():
    with pytest.raises(ConnectorCallError) as exc:
        drv_mod.INVOKE(
            tool_name="upload_file",
            payload={"name": "f", "content_b64": "not!base64@@@"},
            access_token="t",
        )
    assert "base64" in str(exc.value)


def test_upload_file_missing_fields():
    with pytest.raises(ConnectorCallError):
        drv_mod.INVOKE(
            tool_name="upload_file",
            payload={"name": "f"},
            access_token="t",
        )


def test_upload_file_401_raises_auth_expired(monkeypatch):
    stub = _HttpxCapture([_resp_json(401, {"error": "invalid_token"})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    with pytest.raises(ConnectorAuthExpired):
        drv_mod.INVOKE(
            tool_name="upload_file",
            payload={
                "name": "x.txt",
                "content_b64": base64.b64encode(b"x").decode("ascii"),
            },
            access_token="dead",
        )


# ---------- create_folder ---------- #


def test_create_folder_sets_mime_type(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {"id": "FLD_1", "name": "Q3"})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)

    drv_mod.INVOKE(
        tool_name="create_folder",
        payload={"name": "Q3", "parent_id": "ROOT"},
        access_token="t",
    )
    body = stub.calls[0]["json"]
    assert body["name"] == "Q3"
    assert body["mimeType"] == "application/vnd.google-apps.folder"
    assert body["parents"] == ["ROOT"]


def test_create_folder_requires_name():
    with pytest.raises(ConnectorCallError):
        drv_mod.INVOKE(tool_name="create_folder", payload={}, access_token="t")


# ---------- copy_file ---------- #


def test_copy_file_with_new_name(monkeypatch):
    stub = _HttpxCapture([_resp_json(200, {"id": "F_COPY", "name": "Renamed"})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    drv_mod.INVOKE(
        tool_name="copy_file",
        payload={"file_id": "F_ORIG", "new_name": "Renamed", "parent_id": "DEST"},
        access_token="t",
    )
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/files/F_ORIG/copy")
    assert call["json"]["name"] == "Renamed"
    assert call["json"]["parents"] == ["DEST"]


def test_copy_file_no_overrides(monkeypatch):
    """copy_file with just file_id sends an empty body (Drive defaults
    to same-name copy in same folder)."""
    stub = _HttpxCapture([_resp_json(200, {"id": "F2"})])
    monkeypatch.setattr("connectors.google_drive.httpx.request", stub)
    drv_mod.INVOKE(
        tool_name="copy_file",
        payload={"file_id": "F1"},
        access_token="t",
    )
    # json= None means no body — Drive accepts this.
    assert stub.calls[0]["json"] is None


def test_copy_file_requires_file_id():
    with pytest.raises(ConnectorCallError):
        drv_mod.INVOKE(tool_name="copy_file", payload={}, access_token="t")


# ---------- Dispatcher ---------- #


def test_invoke_unknown_tool_raises():
    with pytest.raises(ConnectorCallError) as exc:
        drv_mod.INVOKE(tool_name="bogus", payload={}, access_token="t")
    assert "bogus" in str(exc.value)


# ---------- 20.1 stub-test sweep: no more stubs ---------- #


def test_no_more_stubbed_connectors_in_registry():
    """After 20.5, all three v1 connectors should have real (non-stub)
    invoke functions. Defense against accidentally leaving a stub in
    when adding a new connector to the registry."""
    from connectors import ConnectorNotImplementedError
    for name, spec in CONNECTOR_REGISTRY.items():
        try:
            spec.invoke(tool_name="__nonexistent_tool__", payload={}, access_token="t")
        except ConnectorNotImplementedError:
            pytest.fail(f"connector {name!r} still has the 20.1 stub invoke")
        except ConnectorCallError:
            # Expected: real invoke + unknown tool → ConnectorCallError.
            pass
