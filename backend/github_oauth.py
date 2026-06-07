"""GitHub OAuth web flow (Phase 10B.1).

Replaces the Phase 10 personal-access-token paste with GitHub's classic
OAuth web flow, mirroring `google_oauth.py`:

- `build_authorization_url(state, redirect_uri, scopes)`: the URL the
  user gets redirected to on "Connect GitHub".
- `exchange_code_for_token(code, redirect_uri)`: back-channel exchange of
  the authorization code for an access token.

GitHub's web flow is simpler than Google's: no PKCE (that's for GitHub
Apps / public clients), just a `state` for CSRF and the client secret on
the exchange. The resulting access token is interchangeable with a PAT
everywhere `github_api.py` uses it, so the rest of Phase 10 (webhook
verify, Polaris repo reads, push-to-deploy) needs no change.

Config (env):
  LIGHTSEI_GITHUB_CLIENT_ID, LIGHTSEI_GITHUB_CLIENT_SECRET   OAuth App creds
  LIGHTSEI_GITHUB_REDIRECT_URI                               callback URL
"""
import logging
import os
import secrets as _stdlib_secrets
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("lightsei.github_oauth")

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

# `repo` covers private repo contents + webhooks, which is what
# push-to-deploy + Polaris doc reads need. read:user lets us label the
# connection with the GitHub login that authorized it.
DEFAULT_SCOPES = ["repo", "read:user"]

DEFAULT_REDIRECT_URI = "https://api.lightsei.com/github/oauth/callback"


def _client_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_GITHUB_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.environ.get("LIGHTSEI_GITHUB_CLIENT_SECRET")


def default_redirect_uri() -> str:
    return os.environ.get("LIGHTSEI_GITHUB_REDIRECT_URI", DEFAULT_REDIRECT_URI)


def is_configured() -> bool:
    """True when both OAuth App credentials are present. The start
    endpoint checks this so we never redirect a user into a half-
    configured flow that dead-ends at the callback."""
    return bool(_client_id() and _client_secret())


def new_state() -> str:
    """Random CSRF state token. Stored server-side in oauth_pending_states
    bound to the workspace; the callback rejects any state it didn't
    issue."""
    return _stdlib_secrets.token_urlsafe(32)


def build_authorization_url(
    *,
    state: str,
    redirect_uri: Optional[str] = None,
    scopes: Optional[list[str]] = None,
) -> str:
    """Assemble the GitHub authorize URL the user is redirected to."""
    redirect_uri = redirect_uri or default_redirect_uri()
    params = {
        "client_id": _client_id() or "",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes or DEFAULT_SCOPES),
        "state": state,
        # Don't offer a "create a GitHub account" path mid-connect.
        "allow_signup": "false",
    }
    return f"{GITHUB_AUTH_URL}?{urlencode(params)}"


class GitHubOAuthError(Exception):
    """Surfaced from exchange_code_for_token when the back-channel fails:
    GitHub said no, the network broke, or the response lacked a token.
    The handler in main.py converts this to a user-facing "connect
    failed, try again" state."""


def exchange_code_for_token(
    *,
    code: str,
    redirect_uri: Optional[str] = None,
) -> str:
    """Exchange the authorization code for an access token.

    Returns the access token string. Raises GitHubOAuthError on any
    failure (transport, GitHub error response, or a 200 with no token).
    """
    if not is_configured():
        raise GitHubOAuthError("github_oauth not configured")

    redirect_uri = redirect_uri or default_redirect_uri()

    try:
        resp = httpx.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": _client_id() or "",
                "client_secret": _client_secret() or "",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            # GitHub returns form-encoded by default; ask for JSON so we
            # get a clean {access_token, scope, token_type} or {error}.
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("github_oauth: token exchange transport failed")
        raise GitHubOAuthError(f"token exchange failed: {exc}") from exc

    if resp.status_code >= 400:
        logger.error("github_oauth: token exchange HTTP %s", resp.status_code)
        raise GitHubOAuthError(
            f"token exchange returned {resp.status_code}"
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise GitHubOAuthError("token response was not JSON") from exc

    # GitHub signals failure with a 200 + {error, error_description}.
    if body.get("error"):
        logger.error("github_oauth: token exchange error %s", body.get("error"))
        raise GitHubOAuthError(f"github error: {body.get('error')}")

    token = body.get("access_token")
    if not token or not isinstance(token, str):
        raise GitHubOAuthError("token response had no access_token")
    return token
