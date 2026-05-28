"""Phase 29.2c: Sign in with Apple identity-token verifier.

The iOS app's `signInWithApple()` flow hands the backend an
identity token (a JWT signed by Apple). This module verifies the
signature against Apple's public JWKS + the claims (iss, aud, exp)
+ returns the `sub` / `email` for account lookup.

Tri-state env (parallel to push.py + email_provider):

  - LIGHTSEI_APPLE_SIWA_BUNDLE_ID: audience to validate against
    (default: com.lightsei.app).
  - LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE: when set, refuses capture-
    mode shortcuts so a prod misconfig surfaces loud.

JWKS cache: Apple rotates these keys infrequently. We cache the
fetched JWKS in-process for 24h; on a kid miss (rare, only on
rotation) we force-refresh.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm


log = logging.getLogger("lightsei.siwa")

DEFAULT_BUNDLE_ID = "com.lightsei.app"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"
JWKS_TTL_SECONDS = 24 * 60 * 60  # 24h

# Module-level JWKS cache. ((fetched_at, jwks_dict)) tuple; reset to
# None on init + on a kid miss to force a re-fetch.
_jwks_cache: Optional[tuple[float, dict[str, Any]]] = None


@dataclass(frozen=True)
class AppleIdentityClaim:
    sub: str            # Apple's opaque stable user id
    email: Optional[str]
    email_verified: bool


def _bundle_id() -> str:
    return os.environ.get(
        "LIGHTSEI_APPLE_SIWA_BUNDLE_ID", DEFAULT_BUNDLE_ID,
    )


def _require_live() -> bool:
    return os.environ.get("LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE", "") in {
        "1", "true", "yes",
    }


class SiwaNotConfiguredError(RuntimeError):
    """Raised when the endpoint is hit without the SIWA verify path
    being live. Distinct from a token-validation failure so
    observability can separate misconfig from attack attempts."""


class SiwaInvalidTokenError(RuntimeError):
    """The identity token failed validation. Distinct type so the
    endpoint returns 401 rather than 500."""


def _fetch_jwks(*, force: bool = False) -> dict[str, Any]:
    """Fetch + cache Apple's JWKS. The keys rotate infrequently;
    24h cache keeps us out of trouble + a force=True kicks the
    cache when a token's kid isn't in the cached set."""
    global _jwks_cache
    now = time.time()
    if (
        not force
        and _jwks_cache is not None
        and now - _jwks_cache[0] < JWKS_TTL_SECONDS
    ):
        return _jwks_cache[1]
    log.info("siwa: fetching JWKS from %s", APPLE_JWKS_URL)
    try:
        resp = httpx.get(APPLE_JWKS_URL, timeout=5.0)
        resp.raise_for_status()
        jwks = resp.json()
    except Exception as e:
        raise SiwaInvalidTokenError(
            f"could not fetch Apple JWKS: {e}",
        ) from e
    _jwks_cache = (now, jwks)
    return jwks


def _find_key(jwks: dict[str, Any], kid: str) -> Optional[dict[str, Any]]:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def _reset_cache_for_tests() -> None:
    """Test-only: clear the JWKS cache so tests with monkeypatched
    httpx see a fresh fetch."""
    global _jwks_cache
    _jwks_cache = None


def verify_identity_token(token: str) -> AppleIdentityClaim:
    """Verify the iOS-supplied identity token + return its claims.

    Raises SiwaNotConfiguredError when REQUIRE_LIVE isn't set (the
    endpoint should 501 rather than create accounts off unverified
    tokens). Raises SiwaInvalidTokenError on signature failure or
    bad claims (the endpoint should 401)."""
    if not _require_live():
        raise SiwaNotConfiguredError(
            "siwa: LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE is not set; "
            "set it to 1 once Apple Developer setup is complete",
        )

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise SiwaInvalidTokenError(f"token header invalid: {e}") from e

    kid = header.get("kid")
    if not kid:
        raise SiwaInvalidTokenError("token header missing 'kid'")

    jwks = _fetch_jwks()
    key = _find_key(jwks, kid)
    if key is None:
        # Apple rotated keys since our last fetch — refresh + retry.
        jwks = _fetch_jwks(force=True)
        key = _find_key(jwks, kid)
    if key is None:
        raise SiwaInvalidTokenError(
            f"no Apple public key matches kid={kid!r}",
        )

    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(key))
    except (jwt.PyJWTError, ValueError) as e:
        raise SiwaInvalidTokenError(
            f"could not parse Apple JWK: {e}",
        ) from e

    try:
        payload = jwt.decode(
            token,
            key=public_key,
            algorithms=[key.get("alg", "RS256")],
            audience=_bundle_id(),
            issuer=APPLE_ISSUER,
        )
    except jwt.PyJWTError as e:
        raise SiwaInvalidTokenError(
            f"identity token failed validation: {e}",
        ) from e

    sub = payload.get("sub")
    if not sub:
        raise SiwaInvalidTokenError("token missing 'sub' claim")

    return AppleIdentityClaim(
        sub=sub,
        # Apple only includes email on the first sign-in; subsequent
        # sign-ins from the same Apple ID omit it. Caller falls back
        # to the iOS-cached email forwarded via the body.
        email=payload.get("email"),
        # `email_verified` arrives as the string "true"/"false" in
        # some token versions; normalize.
        email_verified=str(
            payload.get("email_verified", "false"),
        ).lower() == "true",
    )
