"""HTTP-out helper for notification dispatchers.

Shared by every formatter's post(). Centralizing it means failure
shapes are identical across platforms (status code captured, body
snippet trimmed, timeout handled), so the dashboard's
notification_deliveries view doesn't need per-platform branches when
rendering errors.

Per phase plan: 2s timeout, no retries. Webhook providers have their
own delivery story; our audit trail is the durable record.
"""
import logging
from typing import Any, Optional

import httpx

from ._types import Delivery

logger = logging.getLogger("lightsei.notifications")

REQUEST_TIMEOUT_S = 2.0
RESPONSE_BODY_PREVIEW_CHARS = 500


def post_json(
    *,
    url: str,
    body: dict[str, Any],
    extra_headers: Optional[dict[str, str]] = None,
) -> Delivery:
    """POST a JSON body to a webhook URL. Returns a Delivery — never
    raises. The Phase 9.1 audit row gets `status='sent'` for any
    2xx response and `status='failed'` for everything else (4xx, 5xx,
    timeout, connection error)."""
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            r = client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        return _timeout(exc)
    except httpx.HTTPError as exc:
        return _transport_error(exc)
    except Exception as exc:  # belt-and-suspenders
        logger.exception("notification post crashed")
        return _post_exception(exc)

    return _delivery_from_response(r)


def post_raw(
    *,
    url: str,
    content: bytes,
    headers: dict[str, str],
) -> Delivery:
    """Post a pre-serialized body. Same error mapping as post_json,
    but the caller controls the bytes that hit the wire — required
    by the webhook formatter so the HMAC signing input matches the
    request body exactly. Caller must include `Content-Type` in
    headers; we don't second-guess it."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            r = client.post(url, content=content, headers=headers)
    except httpx.TimeoutException as exc:
        return _timeout(exc)
    except httpx.HTTPError as exc:
        return _transport_error(exc)
    except Exception as exc:
        logger.exception("notification post crashed")
        return _post_exception(exc)

    return _delivery_from_response(r)


# ---------- shared response/error mapping ---------- #


def _delivery_from_response(r: "httpx.Response") -> Delivery:
    body_preview = (r.text or "")[:RESPONSE_BODY_PREVIEW_CHARS]
    if 200 <= r.status_code < 300:
        return Delivery(
            status="sent",
            response_summary={
                "http_status": r.status_code,
                "response_preview": body_preview,
            },
        )
    return Delivery(
        status="failed",
        response_summary={
            "error": "http_error",
            "http_status": r.status_code,
            "response_preview": body_preview,
        },
    )


def _timeout(exc: Exception) -> Delivery:
    return Delivery(
        status="failed",
        response_summary={
            "error": "timeout",
            "message": f"request did not complete within {REQUEST_TIMEOUT_S}s: {exc}",
        },
    )


def _transport_error(exc: Exception) -> Delivery:
    return Delivery(
        status="failed",
        response_summary={
            "error": "transport_error",
            "message": f"{type(exc).__name__}: {exc}",
        },
    )


def _post_exception(exc: Exception) -> Delivery:
    return Delivery(
        status="failed",
        response_summary={
            "error": "post_exception",
            "message": f"{type(exc).__name__}: {exc}",
        },
    )
