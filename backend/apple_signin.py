"""Phase 29.2c stub: Sign in with Apple identity-token verifier.

Live path (TODO when Bailey gets the Apple Developer account):

  1. Fetch Apple's JWKS from https://appleid.apple.com/auth/keys
     (cache the response for ~24h; rotates infrequently).
  2. Decode the iOS-app-supplied identity token (JWT) with the
     matching key. Verify:
       - signature
       - iss == "https://appleid.apple.com"
       - aud == LIGHTSEI_APPLE_SIWA_BUNDLE_ID (e.g. com.lightsei.app)
       - exp > now
  3. Return AppleIdentityClaim(email, sub, email_verified). The
     `sub` is Apple's stable opaque user id; the email may be a
     private relay address (@privaterelay.appleid.com) — that's
     fine, Lightsei uses the same email-as-identity contract as
     magic-link.
  4. Apple only sends `email` on the FIRST signin. Subsequent
     signins return just `sub`; the iOS app should pass the email
     it received originally (it sees it via ASAuthorizationCredential
     fullName + email on the first auth). Backend stores email per
     EndUser; sub-based EndUser lookup is the lookup key.

This module is currently a capture-mode stub. The endpoint hands the
identity token through `verify_identity_token`; without
LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE set, the endpoint returns 501 +
clear "not configured" rather than letting capture-mode flow create
real accounts. Same tri-state pattern as push.py and email_provider:

  - keys + REQUIRE_LIVE not set    → 501 "siwa not configured"
  - REQUIRE_LIVE=1 but live JWKS validation TODO → 501 with
    a different hint so misconfig surfaces before silent failure.

Env vars (none required today):

  - LIGHTSEI_APPLE_SIWA_BUNDLE_ID: the audience to validate against
    (default: com.lightsei.app).
  - LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE: when set, the endpoint refuses
    capture-mode shortcuts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


DEFAULT_BUNDLE_ID = "com.lightsei.app"


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


def verify_identity_token(token: str) -> AppleIdentityClaim:
    """Verify the iOS-supplied identity token + return its claims.

    TODO(29.2c live): fetch JWKS, verify JWT signature + claims,
    return real AppleIdentityClaim. Today: raises
    SiwaNotConfiguredError so the endpoint can fall through to
    501 without ever creating EndUser rows for unverified tokens.
    """
    if not _require_live():
        raise SiwaNotConfiguredError(
            "siwa: LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE is not set; "
            "the verify path is stubbed pending Apple Developer "
            "account setup",
        )
    # Live path not implemented yet — same tri-state safety as
    # push.py: REQUIRE_LIVE without an actual implementation is
    # treated as misconfig, not a silent pass.
    raise SiwaNotConfiguredError(
        "siwa: LIGHTSEI_APPLE_SIWA_REQUIRE_LIVE=1 but JWKS "
        "verification isn't implemented yet. Implement "
        "apple_signin.verify_identity_token before flipping on.",
    )
