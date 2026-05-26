"""Phase 29.4 stub: APNS push delivery.

Parallel to push.py (web push). The Phase 28.3 helper
`_push_notify_end_user_if_subscribed` fans out across both Web Push
subscriptions (via push.send_to_end_user) and APNS device tokens
(via this module's send_to_end_user).

Live path (TODO when Bailey gets the Apple Developer account +
generates an APNS auth key .p8):

  1. Sign a JWT with the .p8 key (ES256). Claims:
       iss = LIGHTSEI_APNS_TEAM_ID
       iat = now
       kid = LIGHTSEI_APNS_KEY_ID (header)
     Cache the JWT for ~50 minutes; APNS rejects tokens > 60min.
  2. Pick the gateway URL based on the row's `environment` column:
       sandbox    → https://api.sandbox.push.apple.com
       production → https://api.push.apple.com
  3. POST /3/device/{device_token} with:
       authorization: bearer <jwt>
       apns-topic: <row.bundle_id>
       apns-push-type: alert
       body: {"aps": {"alert": {"title": title, "body": body},
                       "sound": "default"},
              "deep_link_url": deep_link_url}
  4. On 410 (BadDeviceToken / Unregistered): set revoked_at so the
     row falls out of the active fan-out (same pattern as
     push.py's 410 handling).
  5. Bump last_used_at on success.

Today this module is a capture-mode stub: every send is captured
to `_captured` so tests can assert on the fan-out shape without an
APNS connection.

Env vars (none required today):

  - LIGHTSEI_APNS_TEAM_ID: Apple Developer Team ID.
  - LIGHTSEI_APNS_KEY_ID: 10-char .p8 key id.
  - LIGHTSEI_APNS_PRIVATE_KEY: PEM-encoded ES256 private key.
  - LIGHTSEI_APNS_REQUIRE_LIVE: when set, the sender errors loud
    on missing keys instead of silent capture.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import EndUserApnsToken


log = logging.getLogger("lightsei.apns")

_captured: list[dict[str, Any]] = []


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
    """Test/debug accessor matching push.py's shape."""
    return list(_captured)


def _reset_for_tests() -> None:
    _captured.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    `end_user_id`. Parallel to push.send_to_end_user (web push).

    Today: capture mode only. Live path (JWT signing + HTTP/2 POST
    to APNS) is a TODO in this module's docstring.
    """
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

    payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
        },
    }
    if deep_link_url:
        payload["deep_link_url"] = deep_link_url

    if not _is_live():
        # Capture mode: record what would have been sent + bump
        # last_used_at to match the live-path observable state.
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

    # TODO(29.4 live): sign JWT, pick gateway, POST, handle 410.
    # The shape below is illustrative — pending real implementation.
    log.error(
        "apns: live-send path not implemented yet but APNS keys "
        "are set; refusing to silently drop %d push(es)",
        len(rows),
    )
    raise ApnsNotConfiguredError(
        "apns: live-send path not implemented yet",
    )
