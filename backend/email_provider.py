"""Phase 17.2: Resend transactional email integration.

Pure module — single function `send_magic_link(email, token, dashboard_url)`
that either posts to Resend's API or, when running without the API key
set (CI / dev), captures the email in an in-process list so tests can
assert on it without hitting the network.

Configurable via:
  - `LIGHTSEI_RESEND_API_KEY`: Resend secret. When unset, the module
    falls into capture mode automatically. Same fail-open shape the
    rest of the SDK uses — local dev / CI shouldn't break because
    nobody set up the email provider yet.
  - `LIGHTSEI_EMAIL_FROM`: From-address Resend sends as. Defaults to
    `noreply@lightsei.com`. Domain has to be verified on Resend for
    sends to land in inboxes; default keeps everything tied to one
    DNS-managed domain.
  - `LIGHTSEI_EMAIL_REQUIRE_LIVE`: prod safety switch. When truthy,
    a missing API key is a hard error instead of a silent fall into
    capture mode. Set on prod so a misconfigured deploy surfaces
    immediately rather than swallowing every magic-link send. Tests
    can still force capture via LIGHTSEI_EMAIL_FAKE_CAPTURE, which
    wins over REQUIRE_LIVE.

No retries on send failure in v1. A flaky email send means the user
re-triggers the magic-link request from the dashboard — preferable
to silently double-sending. Switch trigger: if real users hit a flake
that they don't think to retry, add a single retry with backoff.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("lightsei.email")

_RESEND_API_URL = "https://api.resend.com/emails"

DEFAULT_FROM_ADDRESS = "Lightsei <noreply@lightsei.com>"


# Capture list for tests + dev mode. Each entry is the dict that would
# have been POSTed to Resend. Read via `captured_emails()`, cleared via
# `_reset_for_tests()` (called from conftest).
_captured: list[dict[str, Any]] = []


def _api_key() -> Optional[str]:
    return os.environ.get("LIGHTSEI_RESEND_API_KEY")


def _from_address() -> str:
    return os.environ.get("LIGHTSEI_EMAIL_FROM", DEFAULT_FROM_ADDRESS)


def _is_live() -> bool:
    """True when the module is wired to Resend; False when in capture
    mode. Tests can flip this off explicitly via env var override."""
    return bool(_api_key()) and not _fake_capture_forced()


def _fake_capture_forced() -> bool:
    return os.environ.get("LIGHTSEI_EMAIL_FAKE_CAPTURE", "") in {
        "1", "true", "yes",
    }


def _require_live() -> bool:
    return os.environ.get("LIGHTSEI_EMAIL_REQUIRE_LIVE", "") in {
        "1", "true", "yes",
    }


class EmailNotConfiguredError(RuntimeError):
    """Raised when send is attempted with no API key while
    LIGHTSEI_EMAIL_REQUIRE_LIVE is on. Distinct type so observability
    can flag it separately from generic transport / Resend failures."""


def captured_emails() -> list[dict[str, Any]]:
    """Test/debug accessor for emails captured while in capture mode."""
    return list(_captured)


def _reset_for_tests() -> None:
    _captured.clear()


def _render_magic_link_body(magic_url: str) -> tuple[str, str]:
    """Return (html, text) bodies for the magic-link email. Plain text
    is the fallback for clients that don't render HTML — also what
    Resend's preview shows in their dashboard."""
    text = (
        "Sign in to Lightsei\n\n"
        f"Click the link below to sign in. It works once and "
        f"expires in 15 minutes.\n\n"
        f"{magic_url}\n\n"
        "If you didn't request this, ignore this email."
    )
    html = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; padding: 24px;">
    <h2 style="margin: 0 0 16px; font-weight: 600;">Sign in to Lightsei</h2>
    <p style="margin: 0 0 16px;">
      Click the button below to sign in. It works once and expires in
      15 minutes.
    </p>
    <p style="margin: 0 0 24px;">
      <a href="{magic_url}"
         style="display: inline-block; padding: 10px 18px;
                background: #6366f1; color: #ffffff; border-radius: 6px;
                text-decoration: none; font-weight: 500;">
        Sign in
      </a>
    </p>
    <p style="margin: 0; color: #6b7280; font-size: 13px;">
      Or paste this URL into your browser:<br>
      <span style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all;">{magic_url}</span>
    </p>
    <p style="margin: 24px 0 0; color: #6b7280; font-size: 13px;">
      If you didn't request this, ignore this email.
    </p>
  </body>
</html>"""
    return html, text


def send_magic_link(
    email: str, token: str, dashboard_url: str,
) -> None:
    """Send a magic-link email. `dashboard_url` is the base of the
    dashboard's magic-link landing page (`https://app.lightsei.com`).

    Raises an exception on transport / non-2xx in live mode; the
    request endpoint catches and 503s. In capture mode never raises —
    appending to an in-process list can't fail.
    """
    magic_url = f"{dashboard_url.rstrip('/')}/auth/magic-link?token={token}"
    html, text = _render_magic_link_body(magic_url)
    payload = {
        "from": _from_address(),
        "to": [email],
        "subject": "Sign in to Lightsei",
        "text": text,
        "html": html,
    }
    _send(payload, email=email, magic_url=magic_url)


def _render_end_user_magic_link_body(magic_url: str) -> tuple[str, str]:
    """Phase 25.2: distinct copy for end-user magic links so the
    consumer-facing surface reads like a consumer product, not the
    operator's developer tool. Same 15-minute TTL + single-use rules.
    """
    text = (
        "Sign in to your account\n\n"
        "Tap the link below to sign in. It works once and expires in "
        "15 minutes.\n\n"
        f"{magic_url}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )
    html = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; padding: 24px;">
    <h2 style="margin: 0 0 16px; font-weight: 600;">Sign in to your account</h2>
    <p style="margin: 0 0 16px;">
      Tap the button below to sign in. It works once and expires in
      15 minutes.
    </p>
    <p style="margin: 0 0 24px;">
      <a href="{magic_url}"
         style="display: inline-block; padding: 10px 18px;
                background: #6366f1; color: #ffffff; border-radius: 6px;
                text-decoration: none; font-weight: 500;">
        Sign in
      </a>
    </p>
    <p style="margin: 0; color: #6b7280; font-size: 13px;">
      Or paste this URL into your browser:<br>
      <span style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all;">{magic_url}</span>
    </p>
    <p style="margin: 24px 0 0; color: #6b7280; font-size: 13px;">
      If you didn't request this, you can safely ignore this email.
    </p>
  </body>
</html>"""
    return html, text


def send_end_user_magic_link(
    email: str, token: str, dashboard_url: str,
) -> None:
    """Phase 25.2: send an end-user magic-link email. Same live /
    capture / REQUIRE_LIVE machinery as `send_magic_link` but with
    consumer-friendly copy and the end-user landing-page URL.

    `dashboard_url` is the base of the dashboard host
    (`https://app.lightsei.com`); the token lands on
    `/auth/end-user/magic-link?token=...` which the Phase 26.2
    end-user consume page POSTs back to the backend.
    """
    magic_url = (
        f"{dashboard_url.rstrip('/')}"
        f"/auth/end-user/magic-link?token={token}"
    )
    html, text = _render_end_user_magic_link_body(magic_url)
    payload = {
        "from": _from_address(),
        "to": [email],
        "subject": "Sign in to your account",
        "text": text,
        "html": html,
    }
    _send(payload, email=email, magic_url=magic_url)


def _send(
    payload: dict[str, Any], *, email: str, magic_url: str,
) -> None:
    """Shared live-vs-capture dispatch for transactional emails.

    Lifted out of `send_magic_link` in Phase 25.2 so the new end-user
    send shares the FAKE_CAPTURE + REQUIRE_LIVE rules without duplicating
    them. Captured rows preserve `_magic_url` for tests that simulate
    the user clicking the link.
    """

    if not _is_live():
        # FAKE_CAPTURE wins unconditionally: tests + local dev rely on
        # it to stay network-free even on a prod-configured deploy.
        if not _fake_capture_forced() and _require_live():
            logger.error(
                "email: LIGHTSEI_EMAIL_REQUIRE_LIVE=true but "
                "LIGHTSEI_RESEND_API_KEY is missing — refusing to "
                "silently capture send to %s",
                email,
            )
            raise EmailNotConfiguredError(
                "LIGHTSEI_RESEND_API_KEY is required when "
                "LIGHTSEI_EMAIL_REQUIRE_LIVE is set"
            )
        # Capture mode: short-circuit before the network call. Used by
        # tests + local dev (no LIGHTSEI_RESEND_API_KEY set). The
        # captured entry preserves the magic_url so tests can simulate
        # the user clicking the link.
        _captured.append({**payload, "_magic_url": magic_url})
        logger.info(
            "email: captured (no live Resend key) — to=%s magic_url=%s",
            email, magic_url,
        )
        return

    try:
        r = httpx.post(
            _RESEND_API_URL,
            json=payload,
            headers={
                "authorization": f"Bearer {_api_key()}",
                "content-type": "application/json",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("email: Resend POST failed for %s", email)
        raise RuntimeError(f"email send failed: {exc}") from exc

    if r.status_code >= 400:
        logger.error(
            "email: Resend returned %s for %s: %s",
            r.status_code, email, r.text[:200],
        )
        raise RuntimeError(
            f"email send failed: Resend returned {r.status_code}"
        )
    logger.info("email: sent magic-link to %s", email)
