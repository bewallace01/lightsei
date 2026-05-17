"""Phase 17.3: Google OAuth helper.

Pure module — owns the OAuth 2.0 authorization-code + PKCE shape so
the request handlers in main.py stay thin. Three exported surfaces:

- `new_pkce_pair()`: generate a fresh (verifier, challenge) pair.
- `build_authorization_url(state, challenge, redirect_uri)`: assemble
  the URL the user gets redirected to.
- `exchange_code_for_userinfo(code, verifier, redirect_uri)`: do the
  back-channel token-exchange + userinfo fetch, return `{sub, email,
  email_verified, name}`.

Configured via env:
  - LIGHTSEI_GOOGLE_CLIENT_ID, LIGHTSEI_GOOGLE_CLIENT_SECRET:
    OAuth client credentials from Google Cloud Console.
  - LIGHTSEI_GOOGLE_REDIRECT_URI: the dashboard's callback URL
    (defaults to https://app.lightsei.com/auth/google/callback).
    Has to match the URI registered on the Google client exactly.

Tests stub `httpx.post` + `httpx.get` so they don't hit Google's
endpoints; production needs the Google Cloud OAuth client + consent
screen configured before /auth/google/start can complete.
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

logger = logging.getLogger("lightsei.google_oauth")


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Minimum scopes for "who is this user." openid is required for ID
# token; email + profile gets us name + email + verified flag.
DEFAULT_SCOPES = ["openid", "email", "profile"]


def _client_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_GOOGLE_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.environ.get("LIGHTSEI_GOOGLE_CLIENT_SECRET")


def default_redirect_uri() -> str:
    return os.environ.get(
        "LIGHTSEI_GOOGLE_REDIRECT_URI",
        "https://app.lightsei.com/auth/google/callback",
    )


def is_configured() -> bool:
    """True when the Google Cloud OAuth client is wired. The /auth/
    google/start endpoint 503s if not, to fail loud rather than
    redirecting the user into a half-configured flow that errors out
    on Google's end."""
    return bool(_client_id() and _client_secret())


def new_pkce_pair() -> tuple[str, str]:
    """PKCE: generate a high-entropy `code_verifier`, return it +
    the URL-safe base64 SHA-256 challenge derived from it.

    The verifier stays on the server (in oauth_pending_states); the
    challenge goes to Google. On callback we send the verifier with
    the code so Google can re-derive the challenge and check it
    matches the one it stored against the state.
    """
    # 43-128 byte verifier per RFC 7636. 64 bytes urlsafe-base64 gives
    # ~86 chars after stripping padding — well within range.
    verifier = _stdlib_secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def new_state() -> str:
    """Random state token. Server stores it alongside the verifier;
    Google echoes it on callback so we can rebind."""
    return _stdlib_secrets.token_urlsafe(32)


def build_authorization_url(
    *,
    state: str,
    challenge: str,
    redirect_uri: Optional[str] = None,
    scopes: Optional[list[str]] = None,
) -> str:
    """Assemble the URL the user gets redirected to."""
    redirect_uri = redirect_uri or default_redirect_uri()
    params = {
        "client_id": _client_id() or "",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # access_type=offline would let us request a refresh token —
        # not needed in v1 (we only want identity, not ongoing API
        # access). Skipping keeps the consent screen friendlier.
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


class GoogleOAuthError(Exception):
    """Surfaced from exchange_code_for_userinfo when the back-channel
    fails — Google said no, the network broke, the userinfo response
    was malformed. The handler in main.py converts to a 400 and the
    dashboard renders a "sign-in failed, try again" state."""


def exchange_code_for_userinfo(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Exchange the authorization code for tokens, fetch userinfo,
    return the relevant claims. Raises GoogleOAuthError on any failure.

    Return shape:
        {
            "sub": "<google's stable user id>",
            "email": "<primary email>",
            "email_verified": True/False,
            "name": "<display name>" (optional)
        }
    """
    if not is_configured():
        raise GoogleOAuthError("google_oauth not configured")

    redirect_uri = redirect_uri or default_redirect_uri()

    # Step 1: code → tokens.
    try:
        token_response = httpx.post(
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
        logger.exception("google_oauth: token exchange transport failed")
        raise GoogleOAuthError(f"token exchange failed: {exc}") from exc

    if token_response.status_code >= 400:
        # Google returns a JSON body with {error, error_description};
        # log the description but don't leak it to the user.
        try:
            body = token_response.json()
        except Exception:
            body = {"_raw": token_response.text[:200]}
        logger.warning(
            "google_oauth: token exchange %s: %s",
            token_response.status_code, body,
        )
        raise GoogleOAuthError(
            f"token exchange returned {token_response.status_code}"
        )

    try:
        tokens = token_response.json()
    except Exception as exc:
        raise GoogleOAuthError("token exchange returned malformed JSON") from exc

    access_token = tokens.get("access_token")
    if not access_token:
        raise GoogleOAuthError("token exchange missing access_token")

    # Step 2: access_token → userinfo. Google's OIDC userinfo endpoint
    # returns the relevant claims directly; no need to decode the
    # id_token JWT ourselves.
    try:
        ui_response = httpx.get(
            GOOGLE_USERINFO_URL,
            headers={"authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("google_oauth: userinfo fetch failed")
        raise GoogleOAuthError(f"userinfo fetch failed: {exc}") from exc

    if ui_response.status_code >= 400:
        raise GoogleOAuthError(
            f"userinfo returned {ui_response.status_code}"
        )

    try:
        claims = ui_response.json()
    except Exception as exc:
        raise GoogleOAuthError("userinfo returned malformed JSON") from exc

    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise GoogleOAuthError("userinfo missing sub or email")

    return {
        "sub": str(sub),
        "email": str(email).lower(),
        "email_verified": bool(claims.get("email_verified", False)),
        "name": claims.get("name"),
    }
