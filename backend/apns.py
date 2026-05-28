"""Phase 29.4: APNS push delivery.

Parallel to push.py (web push). The Phase 28.3 helper
`_push_notify_end_user_if_subscribed` fans out across both Web
Push subscriptions (push.send_to_end_user) and APNS device
tokens (this module).

Live signing:

  1. Sign an ES256 JWT with the .p8 (LIGHTSEI_APNS_PRIVATE_KEY).
     Claims: iss=TEAM_ID, iat=now. Header: alg=ES256, kid=KEY_ID.
     Cache the JWT for ~50 minutes — APNS rejects auth tokens
     older than 60min, refreshing more than 50/hr also gets
     throttled.
  2. Pick the gateway by row.environment:
       sandbox    → https://api.sandbox.push.apple.com
       production → https://api.push.apple.com
  3. POST /3/device/{device_token} over HTTP/2 with:
       authorization: bearer <jwt>
       apns-topic: <row.bundle_id>
       apns-push-type: alert
       apns-priority: 10
     Body: {"aps": {"alert": {"title": title, "body": body},
                     "sound": "default"},
            "deep_link_url": deep_link_url?}
  4. 410 Gone (BadDeviceToken / Unregistered) → set
     revoked_at so the row falls out of the active fan-out.
     bumps last_used_at on 2xx.

Env vars (tri-state matches push.py + email_provider):

  - LIGHTSEI_APNS_TEAM_ID: Apple Developer Team ID (10 chars)
  - LIGHTSEI_APNS_KEY_ID: .p8 key id (10 chars)
  - LIGHTSEI_APNS_PRIVATE_KEY: PEM-encoded ES256 private key
  - LIGHTSEI_APNS_FAKE_CAPTURE: force capture mode even when
    keys are set (tests)
  - LIGHTSEI_APNS_REQUIRE_LIVE: refuse capture-mode fallback
    when keys are missing (prod safety)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import EndUserApnsToken


log = logging.getLogger("lightsei.apns")

# Captured pushes for tests + capture mode.
_captured: list[dict[str, Any]] = []

# Module-level JWT cache. Apple rate-limits how often you can
# generate a new provider auth token + the token is valid for
# ~60min, so we cache for 50min to stay safely inside the window.
_jwt_cache: Optional[tuple[float, str]] = None
JWT_TTL_SECONDS = 50 * 60


def _team_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_APNS_TEAM_ID")


def _key_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_APNS_KEY_ID")


def _private_key() -> Optional[str]:
    return os.environ.get("LIGHTSEI_APNS_PRIVATE_KEY")


def _fake_capture_forced() -> bool:
    return os.environ.get("LIGHTSEI_APNS_FAKE_CAPTURE", "") in {
        "1", "true", "yes",
    }


def _require_live() -> bool:
    return os.environ.get("LIGHTSEI_APNS_REQUIRE_LIVE", "") in {
        "1", "true", "yes",
    }


def _is_live() -> bool:
    return (
        bool(_team_id())
        and bool(_key_id())
        and bool(_private_key())
        and not _fake_capture_forced()
    )


class ApnsNotConfiguredError(RuntimeError):
    """Raised when send is attempted with no APNS keys while
    REQUIRE_LIVE is on."""


def captured_pushes() -> list[dict[str, Any]]:
    return list(_captured)


def _reset_for_tests() -> None:
    global _jwt_cache
    _captured.clear()
    _jwt_cache = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provider_jwt() -> str:
    """Sign + cache the APNS provider auth token. iat is renewed
    every JWT_TTL_SECONDS; the same JWT covers all device sends
    in the meantime."""
    global _jwt_cache
    now = time.time()
    if _jwt_cache is not None and now - _jwt_cache[0] < JWT_TTL_SECONDS:
        return _jwt_cache[1]

    import jwt as _jwt
    token = _jwt.encode(
        payload={"iss": _team_id(), "iat": int(now)},
        key=_private_key(),
        algorithm="ES256",
        headers={"alg": "ES256", "kid": _key_id()},
    )
    _jwt_cache = (now, token)
    return token


def _gateway_url(environment: str) -> str:
    if environment == "production":
        return "https://api.push.apple.com"
    return "https://api.sandbox.push.apple.com"


@dataclass
class SendResult:
    sent: int
    failed: int
    revoked: int
    total_subs: int


def send_to_end_user(
    session: Session,
    end_user_id: str,
    *,
    title: str,
    body: str,
    deep_link_url: Optional[str] = None,
) -> SendResult:
    """Fan out an APNS push to every active device-token row for
    `end_user_id`. Parallel to push.send_to_end_user (web push)."""
    if _require_live() and not _is_live():
        log.warning(
            "apns: LIGHTSEI_APNS_REQUIRE_LIVE=true but APNS keys "
            "are missing; failing send loudly",
        )
        raise ApnsNotConfiguredError(
            "APNS keys required when LIGHTSEI_APNS_REQUIRE_LIVE is set",
        )

    rows = list(session.scalars(
        select(EndUserApnsToken)
        .where(
            EndUserApnsToken.end_user_id == end_user_id,
            EndUserApnsToken.revoked_at.is_(None),
        )
    ).all())

    result = SendResult(sent=0, failed=0, revoked=0, total_subs=len(rows))
    if not rows:
        return result

    payload: dict[str, Any] = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
        },
    }
    if deep_link_url:
        payload["deep_link_url"] = deep_link_url

    if not _is_live():
        # Capture mode: record what would have been sent + bump
        # last_used_at so tests asserting on persistence pass.
        for row in rows:
            _captured.append({
                "device_token": row.device_token,
                "bundle_id": row.bundle_id,
                "environment": row.environment,
                "payload": payload,
            })
            row.last_used_at = _now()
            result.sent += 1
        return result

    # Live path: sign JWT + POST each device over HTTP/2.
    import httpx as _httpx

    jwt_token = _provider_jwt()

    # One client for the whole fan-out so connection reuse keeps
    # latency low across multi-device sends.
    with _httpx.Client(http2=True, timeout=10.0) as client:
        for row in rows:
            url = (
                f"{_gateway_url(row.environment)}"
                f"/3/device/{row.device_token}"
            )
            headers = {
                "authorization": f"bearer {jwt_token}",
                "apns-topic": row.bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
            }
            try:
                resp = client.post(url, headers=headers, json=payload)
            except _httpx.HTTPError as e:
                log.warning(
                    "apns: transport error to %s for token=...%s: %s",
                    row.environment, row.device_token[-8:], e,
                )
                result.failed += 1
                continue

            if resp.status_code == 200:
                row.last_used_at = _now()
                result.sent += 1
            elif resp.status_code == 410:
                # BadDeviceToken / Unregistered: device wiped or
                # uninstalled. Soft-revoke so future fan-outs skip.
                row.revoked_at = _now()
                result.revoked += 1
                log.info(
                    "apns: 410 from device, revoking token=...%s",
                    row.device_token[-8:],
                )
            else:
                log.warning(
                    "apns: %d from %s for token=...%s: %s",
                    resp.status_code, row.environment,
                    row.device_token[-8:], resp.text[:200],
                )
                result.failed += 1

    return result
