"""Phase 32.13: Google Business Profile connector tests.

Stubs httpx.request so tests never hit Google. Each tool gets a happy
path; plus 401 -> ConnectorAuthExpired and unknown-tool ->
ConnectorCallError, matching the Gmail connector's coverage.
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
from connectors import google_business as biz_mod


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


# ---------- MANIFEST + registry ---------- #


def test_manifest_lists_four_tools():
    names = {t["name"] for t in biz_mod.MANIFEST()}
    assert names == {
        "list_accounts", "list_locations", "list_reviews", "reply_to_review",
    }


def test_registered_with_wedge_safe_zones():
    spec = CONNECTOR_REGISTRY["google_business"]
    assert spec.oauth_provider == "google"
    assert spec.display_label == "Google Business Profile"
    # Reviewing/replying is a business-account action: never public-zoned.
    assert "public" not in spec.declared_zones
    assert "internal" in spec.declared_zones
    assert any("business.manage" in s for s in spec.default_scopes)


# ---------- list_accounts ---------- #


def test_list_accounts_strips_resource_prefix(monkeypatch):
    stub = _HttpxCapture([
        _resp(200, {"accounts": [
            {"name": "accounts/123", "accountName": "Acme Coffee",
             "type": "LOCATION_GROUP"},
        ]}),
    ])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)

    out = biz_mod.INVOKE(tool_name="list_accounts", payload={}, access_token="t")
    assert out["accounts"][0]["id"] == "123"
    assert out["accounts"][0]["account_name"] == "Acme Coffee"


# ---------- list_locations ---------- #


def test_list_locations_requires_account_id():
    with pytest.raises(ConnectorCallError):
        biz_mod.INVOKE(tool_name="list_locations", payload={}, access_token="t")


def test_list_locations_strips_prefix_and_sends_read_mask(monkeypatch):
    stub = _HttpxCapture([
        _resp(200, {"locations": [
            {"name": "locations/456", "title": "Acme Coffee Downtown"},
        ]}),
    ])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)

    out = biz_mod.INVOKE(
        tool_name="list_locations",
        payload={"account_id": "123"},
        access_token="t",
    )
    assert out["locations"][0]["id"] == "456"
    # The Business Information API rejects calls without a readMask.
    assert stub.calls[0]["params"]["readMask"] == "name,title"


# ---------- list_reviews ---------- #


def test_list_reviews_normalizes_star_rating_and_reply(monkeypatch):
    stub = _HttpxCapture([
        _resp(200, {
            "reviews": [
                {"reviewId": "r1", "starRating": "ONE",
                 "comment": "Slow service", "createTime": "2026-06-01T00:00:00Z",
                 "reviewer": {"displayName": "Dana"}},
                {"reviewId": "r2", "starRating": "FIVE",
                 "comment": "Great!", "createTime": "2026-06-02T00:00:00Z",
                 "reviewer": {"displayName": "Sam"},
                 "reviewReply": {"comment": "Thanks!"}},
            ],
            "averageRating": 3.0,
            "totalReviewCount": 2,
        }),
    ])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)

    out = biz_mod.INVOKE(
        tool_name="list_reviews",
        payload={"account_id": "123", "location_id": "456"},
        access_token="t",
    )
    r1, r2 = out["reviews"]
    assert r1["rating"] == 1 and r1["star_rating"] == "ONE"
    assert r1["has_reply"] is False
    assert r2["rating"] == 5
    assert r2["has_reply"] is True
    assert out["total_review_count"] == 2


def test_list_reviews_unknown_star_rating_is_none(monkeypatch):
    stub = _HttpxCapture([
        _resp(200, {"reviews": [
            {"reviewId": "r3", "starRating": "STAR_RATING_UNSPECIFIED",
             "comment": "?"},
        ]}),
    ])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)

    out = biz_mod.INVOKE(
        tool_name="list_reviews",
        payload={"account_id": "123", "location_id": "456", "max_results": 5},
        access_token="t",
    )
    assert out["reviews"][0]["rating"] is None


# ---------- reply_to_review ---------- #


def test_reply_to_review_puts_comment(monkeypatch):
    stub = _HttpxCapture([
        _resp(200, {"comment": "Sorry about that!",
                    "updateTime": "2026-06-03T00:00:00Z"}),
    ])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)

    out = biz_mod.INVOKE(
        tool_name="reply_to_review",
        payload={"account_id": "123", "location_id": "456",
                 "review_id": "r1", "comment": "Sorry about that!"},
        access_token="t",
    )
    assert stub.calls[0]["method"] == "PUT"
    assert stub.calls[0]["json"]["comment"] == "Sorry about that!"
    assert out["comment"] == "Sorry about that!"


def test_reply_to_review_requires_all_fields():
    with pytest.raises(ConnectorCallError):
        biz_mod.INVOKE(
            tool_name="reply_to_review",
            payload={"account_id": "123", "location_id": "456"},
            access_token="t",
        )


# ---------- error paths ---------- #


def test_401_raises_auth_expired(monkeypatch):
    stub = _HttpxCapture([_resp(401, {"error": "unauthorized"})])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)
    with pytest.raises(ConnectorAuthExpired):
        biz_mod.INVOKE(tool_name="list_accounts", payload={}, access_token="t")


def test_other_4xx_raises_call_error(monkeypatch):
    stub = _HttpxCapture([_resp(403, {"error": "forbidden"})])
    monkeypatch.setattr("connectors.google_business.httpx.request", stub)
    with pytest.raises(ConnectorCallError):
        biz_mod.INVOKE(tool_name="list_accounts", payload={}, access_token="t")


def test_unknown_tool_raises_call_error():
    with pytest.raises(ConnectorCallError):
        biz_mod.INVOKE(tool_name="nope", payload={}, access_token="t")
