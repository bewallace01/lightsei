"""Phase 32.13: Google Business Profile connector (reviews).

The review source the Reputation assistant needs. Mirrors the Gmail
connector exactly (MANIFEST + INVOKE + a thin _request helper); reuses
the existing Google OAuth flow with the `business.manage` scope.

Tools:
- `list_accounts()` — the Business Profile accounts the connected user
  manages. The first hop to find a location's reviews.
- `list_locations(account_id)` — locations (storefronts) under an account.
- `list_reviews(account_id, location_id, max_results?)` — reviews for a
  location, normalized (star enum -> int rating, reply presence flagged).
- `reply_to_review(account_id, location_id, review_id, comment)` — post the
  owner's reply. The write counterpart, gated like Gmail's send_email.

Google splits this across three host APIs:
- Account Management (accounts list)
- Business Information (locations list)
- the legacy My Business v4 (reviews live only here)

so `_request` takes a full URL rather than one base. Tests stub httpx so
they never hit Google.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from . import ConnectorAuthExpired, ConnectorCallError

logger = logging.getLogger("lightsei.connectors.google_business")


ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
# Reviews were never migrated off the legacy v4 surface.
REVIEWS_BASE = "https://mybusiness.googleapis.com/v4"

# Google returns the star rating as an enum string; the Reputation
# assistant reasons in 1-5. Map both directions of the gap explicitly so
# an unknown/unspecified value becomes None rather than a wrong number.
_STAR_TO_INT = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
}


# ---------- MCP-flavored manifest ---------- #


def MANIFEST() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_accounts",
            "description": (
                "List the Google Business Profile accounts the connected "
                "user manages. Use the returned account id with "
                "list_locations."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "list_locations",
            "description": (
                "List the locations (storefronts) under a Business Profile "
                "account. account_id comes from list_accounts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                },
                "required": ["account_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_reviews",
            "description": (
                "List recent reviews for a location. account_id + location_id "
                "come from list_accounts / list_locations. Returns reviews "
                "with a numeric rating, the comment, the reviewer, and "
                "whether the owner has already replied."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "location_id": {"type": "string"},
                    "max_results": {
                        "type": "integer",
                        "description": "1-50; default 20.",
                    },
                },
                "required": ["account_id", "location_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "reply_to_review",
            "description": (
                "Post (or update) the owner's public reply to a review. "
                "review_id comes from list_reviews."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "location_id": {"type": "string"},
                    "review_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["account_id", "location_id", "review_id", "comment"],
                "additionalProperties": False,
            },
        },
    ]


# ---------- Dispatcher ---------- #


def INVOKE(*, tool_name: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    """Dispatch one tool. Raises ConnectorAuthExpired on 401 so the caller
    refreshes + retries; ConnectorCallError on any other failure."""
    fn = _TOOLS.get(tool_name)
    if fn is None:
        raise ConnectorCallError(
            f"unknown google_business tool {tool_name!r}",
            upstream_status=None,
        )
    return fn(payload, access_token)


# ---------- Per-tool implementations ---------- #


def _strip_prefix(name: Optional[str], prefix: str) -> Optional[str]:
    """'accounts/123' -> '123'. Google returns fully-qualified resource
    names; tools take + return bare ids so call sites stay readable."""
    if not name:
        return name
    return name[len(prefix):] if name.startswith(prefix) else name


def _list_accounts(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    result = _request("GET", f"{ACCOUNTS_BASE}/accounts", access_token)
    accounts = []
    for a in result.get("accounts") or []:
        accounts.append({
            "id": _strip_prefix(a.get("name"), "accounts/"),
            "name": a.get("name"),
            "account_name": a.get("accountName"),
            "type": a.get("type"),
        })
    return {"accounts": accounts}


def _list_locations(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    account_id = payload.get("account_id")
    if not account_id:
        raise ConnectorCallError("list_locations requires account_id")
    # The Business Information API requires an explicit readMask.
    result = _request(
        "GET",
        f"{BUSINESS_INFO_BASE}/accounts/{account_id}/locations",
        access_token,
        params={"readMask": "name,title", "pageSize": 100},
    )
    locations = []
    for loc in result.get("locations") or []:
        locations.append({
            "id": _strip_prefix(loc.get("name"), "locations/"),
            "name": loc.get("name"),
            "title": loc.get("title"),
        })
    return {"locations": locations}


def _normalize_review(r: dict[str, Any]) -> dict[str, Any]:
    reviewer = r.get("reviewer") or {}
    reply = r.get("reviewReply") or {}
    return {
        "id": r.get("reviewId"),
        "rating": _STAR_TO_INT.get(r.get("starRating")),
        "star_rating": r.get("starRating"),
        "comment": r.get("comment") or "",
        "reviewer": reviewer.get("displayName"),
        "create_time": r.get("createTime"),
        "update_time": r.get("updateTime"),
        "has_reply": bool(reply.get("comment")),
    }


def _list_reviews(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    account_id = payload.get("account_id")
    location_id = payload.get("location_id")
    if not (account_id and location_id):
        raise ConnectorCallError("list_reviews requires account_id + location_id")
    page_size = max(1, min(50, int(payload.get("max_results") or 20)))

    result = _request(
        "GET",
        f"{REVIEWS_BASE}/accounts/{account_id}/locations/{location_id}/reviews",
        access_token,
        params={"pageSize": page_size},
    )
    reviews = [_normalize_review(r) for r in (result.get("reviews") or [])]
    return {
        "reviews": reviews,
        "average_rating": result.get("averageRating"),
        "total_review_count": result.get("totalReviewCount")
        or result.get("totalReviewSize"),
    }


def _reply_to_review(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    account_id = payload.get("account_id")
    location_id = payload.get("location_id")
    review_id = payload.get("review_id")
    comment = payload.get("comment")
    if not (account_id and location_id and review_id and comment):
        raise ConnectorCallError(
            "reply_to_review requires account_id + location_id + review_id "
            "+ comment"
        )
    result = _request(
        "PUT",
        f"{REVIEWS_BASE}/accounts/{account_id}/locations/{location_id}"
        f"/reviews/{review_id}/reply",
        access_token,
        json_body={"comment": comment},
    )
    return {
        "comment": result.get("comment"),
        "update_time": result.get("updateTime"),
    }


_TOOLS: dict[str, Any] = {
    "list_accounts": _list_accounts,
    "list_locations": _list_locations,
    "list_reviews": _list_reviews,
    "reply_to_review": _reply_to_review,
}


# ---------- HTTP helper ---------- #


def _request(
    method: str,
    url: str,
    access_token: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """One Google Business Profile API call. Takes a full URL (the reviews
    + accounts + locations endpoints live on three different hosts).

    Raises ConnectorAuthExpired on 401 so the dispatch endpoint can refresh
    + retry; ConnectorCallError on any other non-2xx or transport failure.
    """
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
        logger.exception("google_business: %s %s transport failed", method, url)
        raise ConnectorCallError(
            f"google_business transport error: {exc}"
        ) from exc

    if resp.status_code == 401:
        raise ConnectorAuthExpired("google_business returned 401")

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:300]}
        logger.warning(
            "google_business: %s %s returned %s: %s",
            method, url, resp.status_code, body,
        )
        raise ConnectorCallError(
            f"google_business {method} returned {resp.status_code}",
            upstream_status=resp.status_code,
        )

    try:
        return resp.json() if resp.content else {}
    except Exception as exc:
        raise ConnectorCallError(
            "google_business returned malformed JSON"
        ) from exc
