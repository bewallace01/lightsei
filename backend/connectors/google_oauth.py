"""Phase 20.2: Google OAuth helper for the connector install flow.

Separate from `backend/google_oauth.py` (which handles user sign-in via
Google) because the connector flow has different requirements:

- Per-connector scopes (gmail vs calendar vs drive) — each install
  requests a different scope set defined in `CONNECTOR_REGISTRY`.
- Offline access — `access_type=offline` so Google issues a
  refresh_token. Sign-in doesn't need offline since the session
  token is what matters once the user is in.
- Refresh-token flow — `refresh_access_token()` lets the bot-callable
  endpoint (20.6) renew expired access tokens without re-prompting
  the user.
- Returns the granted scope list — Google lets users decline scopes
  on the consent screen, and we persist what was actually granted
  (not what we asked for) on `connector_installations.scopes`.

Reuses the same `LIGHTSEI_GOOGLE_CLIENT_ID` + `LIGHTSEI_GOOGLE_CLIENT_SECRET`
env vars as sign-in (same OAuth app, just different scopes per call).
Different redirect URI: `LIGHTSEI_CONNECTORS_GOOGLE_REDIRECT_URI` defaults
to `https://api.lightsei.com/connectors/google/callback`.

Tests stub `httpx.post` so they don't hit Google's endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets as _stdlib_secrets
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("lightsei.connectors.google_oauth")


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _client_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_GOOGLE_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.environ.get("LIGHTSEI_GOOGLE_CLIENT_SECRET")


def default_redirect_uri() -> str:
    return os.environ.get(
        "LIGHTSEI_CONNECTORS_GOOGLE_REDIRECT_URI",
        "https://api.lightsei.com/connectors/google/callback",
    )


def is_configured() -> bool:
    """True when the Google OAuth client is wired. Same env vars as
    the sign-in flow — once Google sign-in works, connector OAuth
    works too (modulo the per-connector scope grants the user has
    to consent to on Google's screen)."""
    return bool(_client_id() and _client_secret())


def new_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE verifier + S256 challenge. The verifier stays
    in `connector_oauth_pending_states`; the challenge ships with the
    authorization URL. On callback the verifier is re-sent to Google
    so it can re-derive the challenge + check it matches."""
    verifier = _stdlib_secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def new_state() -> str:
    return _stdlib_secrets.token_urlsafe(32)


def build_authorization_url(
    *,
    state: str,
    challenge: str,
    scopes: list[str],
    redirect_uri: Optional[str] = None,
) -> str:
    """Assemble the Google consent URL for a connector install.

    `access_type=offline` requests a refresh_token (sign-in's URL omits
    this — it doesn't need refresh).
    `prompt=consent` forces the consent screen even on re-install so
    the user can re-approve when scopes change (e.g. adding Drive
    after they only granted Gmail). Without prompt=consent, Google
    sometimes skips the screen and skips issuing a new refresh_token.
    """
    redirect_uri = redirect_uri or default_redirect_uri()
    params = {
        "client_id": _client_id() or "",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


class GoogleConnectorOAuthError(Exception):
    """Raised on transport / 4xx / 5xx / malformed-response from
    Google's OAuth endpoints. The 20.2 callback converts this to an
    HTML 400 with a back-to-integrations link."""


def exchange_code_for_tokens(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Exchange the authorization code for {access_token, refresh_token,
    expires_in, scope, email}. Email is pulled from the id_token's
    payload (Google's exchange endpoint includes an id_token when
    `openid` is among the scopes).

    Raises GoogleConnectorOAuthError on any failure.
    """
    if not is_configured():
        raise GoogleConnectorOAuthError("google connector OAuth not configured")

    redirect_uri = redirect_uri or default_redirect_uri()

    try:
        resp = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _client_id() or "",
                "client_secret": _client_secret() or "",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("connectors.google_oauth: token exchange transport failed")
        raise GoogleConnectorOAuthError(f"token exchange failed: {exc}") from exc

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:200]}
        logger.warning(
            "connectors.google_oauth: token exchange %s: %s",
            resp.status_code, body,
        )
        raise GoogleConnectorOAuthError(
            f"token exchange returned {resp.status_code}"
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise GoogleConnectorOAuthError(
            "token exchange returned malformed JSON"
        ) from exc

    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    scope = body.get("scope") or ""
    id_token = body.get("id_token")

    if not access_token:
        raise GoogleConnectorOAuthError("token exchange missing access_token")
    if not refresh_token:
        # Google sometimes omits refresh_token on re-consent if it has
        # already issued one for the same (user, client, scope set).
        # We need it for refresh; surface as an error so the operator
        # knows to revoke + reinstall to force a fresh refresh_token.
        raise GoogleConnectorOAuthError(
            "token exchange missing refresh_token — "
            "revoke the install on the Google account + reinstall"
        )

    email = _email_from_id_token(id_token) if id_token else None

    return {
        "access_token": str(access_token),
        "refresh_token": str(refresh_token),
        "expires_in": int(expires_in) if isinstance(expires_in, int) else None,
        "scope": str(scope),
        "email": email,
    }


def refresh_access_token(
    *,
    refresh_token: str,
) -> dict[str, Any]:
    """Trade a stored refresh_token for a fresh access_token. Called
    by the 20.6 bot-callable endpoint when a connector call returns
    401 (access_token expired or revoked).

    Returns {access_token, expires_in, scope}. `refresh_token` in the
    response is rare (Google returns the same one); if absent, callers
    keep the existing one.
    """
    if not is_configured():
        raise GoogleConnectorOAuthError("google connector OAuth not configured")

    try:
        resp = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _client_id() or "",
                "client_secret": _client_secret() or "",
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("connectors.google_oauth: refresh transport failed")
        raise GoogleConnectorOAuthError(f"refresh failed: {exc}") from exc

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:200]}
        logger.warning(
            "connectors.google_oauth: refresh %s: %s",
            resp.status_code, body,
        )
        # Google's `invalid_grant` on refresh means the refresh_token
        # was revoked (user disconnected the app from Google's side)
        # OR expired (6 months of inactivity). Either way the install
        # is dead; caller should mark revoked + prompt re-install.
        err = (body or {}).get("error") if isinstance(body, dict) else None
        raise GoogleConnectorOAuthError(
            f"refresh returned {resp.status_code}"
            + (f" ({err})" if err else "")
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise GoogleConnectorOAuthError(
            "refresh returned malformed JSON"
        ) from exc

    access_token = body.get("access_token")
    if not access_token:
        raise GoogleConnectorOAuthError("refresh missing access_token")

    return {
        "access_token": str(access_token),
        "refresh_token": body.get("refresh_token"),  # rare; caller keeps existing if None
        "expires_in": int(body["expires_in"]) if isinstance(body.get("expires_in"), int) else None,
        "scope": str(body.get("scope") or ""),
    }


def _email_from_id_token(id_token: str) -> Optional[str]:
    """Extract the `email` claim from a Google ID token without
    verifying the signature.

    Verification would require fetching Google's JWKS + checking
    expiry. We trust the token here because we received it directly
    from Google's token endpoint over TLS using our own client_secret
    — there's no man-in-the-middle path that could substitute it.
    Sign-in already uses the same shortcut (see backend/google_oauth.py).
    """
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        # Re-pad — base64url payloads from JWTs drop trailing '='.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        import json
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        email = claims.get("email")
        return str(email) if email else None
    except Exception:
        return None
