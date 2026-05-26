"""Phase 28.2: web-push delivery via VAPID.

Pure module wrapping `pywebpush`. One public helper:
`send_to_end_user(session, end_user_id, *, title, body, deep_link_url)`
fans out across active `end_user_push_subscriptions` rows for the
given end user and signs each payload with the workspace VAPID
keys.

Configurable via:
  - `LIGHTSEI_VAPID_PUBLIC_KEY`: base64url-encoded public key
    (also shipped to the frontend at build time; the manifest
    exposes it for `PushManager.subscribe()`).
  - `LIGHTSEI_VAPID_PRIVATE_KEY`: base64url-encoded private key.
    Server-only; signs JWT for each push.
  - `LIGHTSEI_VAPID_SUBJECT`: mailto: URL push services use to
    reach the sender (e.g. `mailto:ops@lightsei.com`). Defaults
    to `mailto:noreply@lightsei.com`.
  - `LIGHTSEI_PUSH_FAKE_CAPTURE`: when truthy, captures the payload
    in-process for tests instead of POSTing to the push service.
    Tests + local dev rely on this to stay network-free.
  - `LIGHTSEI_PUSH_REQUIRE_LIVE`: prod safety switch matching the
    Phase 17 email pattern. When truthy, a missing key is a hard
    error instead of a silent capture.

Mirrors the tri-state shape from `email_provider.py` so the same
behavior applies: REQUIRE_LIVE on prod surfaces misconfig loudly;
FAKE_CAPTURE wins unconditionally for tests; otherwise capture vs
live is decided by whether keys are set.

410 Gone responses from push services mark the subscription
`revoked_at = now()` so the next fan-out skips them. Other
non-2xx responses get logged + skipped (best-effort per Phase 28
spec — `Push delivery is best-effort, not retried`).

Lazy-imports `pywebpush` inside the live-send path so the test
suite + the rest of the backend don't require it at module-import
time. Tests run in capture mode and never touch the library.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import EndUserPushSubscription

logger = logging.getLogger("lightsei.push")

DEFAULT_VAPID_SUBJECT = "mailto:noreply@lightsei.com"


# Capture list for tests + dev. Each entry is the dict that would
# have been signed + POSTed. Read via `captured_pushes()`, cleared
# via `_reset_for_tests()` (called from conftest).
_captured: list[dict[str, Any]] = []


def _public_key() -> Optional[str]:
    return os.environ.get("LIGHTSEI_VAPID_PUBLIC_KEY")


def _private_key() -> Optional[str]:
    return os.environ.get("LIGHTSEI_VAPID_PRIVATE_KEY")


def _subject() -> str:
    return os.environ.get("LIGHTSEI_VAPID_SUBJECT", DEFAULT_VAPID_SUBJECT)


def _fake_capture_forced() -> bool:
    return os.environ.get("LIGHTSEI_PUSH_FAKE_CAPTURE", "") in {
        "1", "true", "yes",
    }


def _require_live() -> bool:
    return os.environ.get("LIGHTSEI_PUSH_REQUIRE_LIVE", "") in {
        "1", "true", "yes",
    }


def _is_live() -> bool:
    """True when both VAPID keys are set + FAKE_CAPTURE is not
    forcing capture mode. False when running in capture mode (tests,
    local dev without keys)."""
    return (
        bool(_public_key())
        and bool(_private_key())
        and not _fake_capture_forced()
    )


class PushNotConfiguredError(RuntimeError):
    """Raised when send is attempted with no VAPID keys while
    LIGHTSEI_PUSH_REQUIRE_LIVE is on. Distinct type so observability
    can flag it separately from generic transport failures."""


def get_vapid_public_key() -> Optional[str]:
    """Public accessor for the VAPID public key. Phase 28.5 surfaces
    this on GET /me/end-user so the /c subscribe flow can pass it to
    `PushManager.subscribe({ applicationServerKey })`. Returns None
    when the key isn't configured (capture mode / local dev), and
    the frontend hides the prompt accordingly."""
    return _public_key()


def captured_pushes() -> list[dict[str, Any]]:
    """Test/debug accessor for pushes captured while in capture mode."""
    return list(_captured)


def _reset_for_tests() -> None:
    _captured.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(
    *,
    title: str,
    body: str,
    deep_link_url: Optional[str],
    icon_url: Optional[str],
) -> dict[str, Any]:
    """Build the JSON payload the service worker push event listener
    reads. Service worker (Phase 28.4) reads `title` + `body` + uses
    `deep_link_url` on `notificationclick`."""
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
    }
    if deep_link_url:
        payload["deep_link_url"] = deep_link_url
    if icon_url:
        payload["icon"] = icon_url
    return payload


def send_to_end_user(
    session: Session,
    end_user_id: str,
    *,
    title: str,
    body: str,
    deep_link_url: Optional[str] = None,
    icon_url: Optional[str] = None,
) -> dict[str, Any]:
    """Fan out a push notification across the end user's active
    subscriptions.

    Returns a summary dict with counts: `sent`, `failed`, `revoked`,
    `total_subs`. Callers can log or fold this into a run-event.

    No-op when the end user has no active subscriptions (returns
    counts all zero, no error). Capture-mode appends each would-be
    payload to `_captured` so tests can inspect; live mode POSTs to
    the push service via pywebpush.

    410 Gone responses set `revoked_at` on the subscription row so
    future fan-outs skip it. Other non-2xx errors get logged + the
    loop continues (best-effort per Phase 28 spec).
    """
    # Snapshot the live VAPID config + capture mode at the start so
    # a mid-run env flip doesn't half-deliver.
    live = _is_live()
    if not live and _require_live() and not _fake_capture_forced():
        logger.error(
            "push: LIGHTSEI_PUSH_REQUIRE_LIVE=true but VAPID keys are "
            "missing — refusing to silently capture push to end_user %s",
            end_user_id,
        )
        raise PushNotConfiguredError(
            "VAPID keys required when LIGHTSEI_PUSH_REQUIRE_LIVE is set"
        )

    payload = _build_payload(
        title=title, body=body,
        deep_link_url=deep_link_url, icon_url=icon_url,
    )
    payload_json = json.dumps(payload)

    subs = session.execute(
        select(EndUserPushSubscription)
        .where(
            EndUserPushSubscription.end_user_id == end_user_id,
            EndUserPushSubscription.revoked_at.is_(None),
        )
    ).scalars().all()

    summary = {
        "sent": 0,
        "failed": 0,
        "revoked": 0,
        "total_subs": len(subs),
    }

    if not subs:
        return summary

    if not live:
        # Capture mode: record each payload + bump last_used_at as
        # if the send had succeeded (tests asserting on side-effects
        # get the same write semantics as live).
        for sub in subs:
            _captured.append({
                "subscription_id": sub.id,
                "end_user_id": sub.end_user_id,
                "endpoint": sub.endpoint,
                "payload": dict(payload),
            })
            sub.last_used_at = _now()
            summary["sent"] += 1
        return summary

    # Live path: import pywebpush lazily so the test suite + the
    # rest of the backend don't pull it in when push isn't
    # configured.
    try:
        from pywebpush import WebPushException, webpush  # type: ignore
    except ImportError as exc:  # pragma: no cover, prod-only branch
        logger.exception(
            "push: pywebpush not installed but LIGHTSEI_VAPID_* keys "
            "are set. Add pywebpush to requirements + redeploy."
        )
        raise PushNotConfiguredError(
            "pywebpush is not installed; cannot send live"
        ) from exc

    vapid_claims = {"sub": _subject()}
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload_json,
                vapid_private_key=_private_key(),
                vapid_claims=dict(vapid_claims),
            )
            sub.last_used_at = _now()
            summary["sent"] += 1
        except WebPushException as exc:  # type: ignore[misc]
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 410:
                # Subscription is dead. Mark revoked + keep the row
                # for audit; future fan-outs skip it via the partial
                # index.
                sub.revoked_at = _now()
                summary["revoked"] += 1
                logger.info(
                    "push: 410 Gone for sub %s (end_user=%s), marking revoked",
                    sub.id, sub.end_user_id,
                )
            else:
                summary["failed"] += 1
                logger.warning(
                    "push: WebPushException for sub %s (end_user=%s): "
                    "status=%s detail=%s",
                    sub.id, sub.end_user_id, status,
                    getattr(exc, "message", str(exc)),
                )
        except Exception:  # pragma: no cover, defensive
            summary["failed"] += 1
            logger.exception(
                "push: unexpected error for sub %s (end_user=%s)",
                sub.id, sub.end_user_id,
            )

    return summary
