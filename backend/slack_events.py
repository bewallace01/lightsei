"""Phase 19.3: Slack events webhook helper.

Owns the signature-verification logic + the small surface that the
`POST /slack/events` endpoint in main.py uses to validate incoming
requests. Separate from `slack_oauth.py` because the OAuth + events
flows have non-overlapping env-var configs (signing secret vs.
client id/secret) and non-overlapping failure modes — keeping them
in separate modules keeps each surface small.

Configured via env:
  - LIGHTSEI_SLACK_SIGNING_SECRET: from the Slack app's basic-info
    page. Slack signs every webhook delivery with this; we verify
    over `v0:{timestamp}:{body}` and reject 400 on mismatch.

The signature scheme is documented at
https://api.slack.com/authentication/verifying-requests-from-slack.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("lightsei.slack_events")


# Slack's documented replay-attack window is 5 minutes — if the request
# timestamp is older than this from "now" we refuse. Generous enough
# that legitimate clock skew between Slack + our backend doesn't reject
# valid deliveries; tight enough that a captured signature can't be
# replayed hours later.
TIMESTAMP_TOLERANCE_S = 300


def signing_secret() -> Optional[str]:
    return os.environ.get("LIGHTSEI_SLACK_SIGNING_SECRET")


def is_signing_configured() -> bool:
    """True when the signing secret is wired. The events webhook 400s
    if not — same pattern as the Stripe webhook secret check: a
    misconfigured endpoint shouldn't 5xx Slack into infinite retries."""
    return bool(signing_secret())


class SlackSignatureError(Exception):
    """Raised when the request signature doesn't verify or the
    timestamp is outside the tolerance window. Handler in main.py
    surfaces as 400 (never 5xx — 5xx makes Slack retry forever)."""


def verify_signature(
    *,
    body: bytes,
    timestamp_header: Optional[str],
    signature_header: Optional[str],
    now_s: Optional[int] = None,
) -> None:
    """Verify Slack's signature on an inbound request.

    Raises SlackSignatureError on:
    - Missing timestamp or signature header.
    - Timestamp not a parseable integer.
    - Timestamp outside the 5-minute tolerance window.
    - Signature doesn't match the HMAC-SHA256 over `v0:{ts}:{body}`.
    - Signing secret env var not set.

    `now_s` is injectable for tests so we don't have to fight a clock.
    """
    secret = signing_secret()
    if not secret:
        raise SlackSignatureError("LIGHTSEI_SLACK_SIGNING_SECRET is not set")

    if not timestamp_header or not signature_header:
        raise SlackSignatureError("missing X-Slack-Signature or X-Slack-Request-Timestamp")

    try:
        timestamp = int(timestamp_header)
    except ValueError:
        raise SlackSignatureError("timestamp header is not an integer")

    current = now_s if now_s is not None else int(time.time())
    if abs(current - timestamp) > TIMESTAMP_TOLERANCE_S:
        # Outside the tolerance window — likely a replay attack OR
        # genuinely-bad clock skew. Either way we don't accept it.
        raise SlackSignatureError("timestamp outside tolerance window")

    # Slack signs over the byte-exact request body, not a re-serialized
    # JSON. We MUST receive raw bytes here, not a parsed dict.
    sig_basestring = b"v0:" + str(timestamp).encode("ascii") + b":" + body
    computed = "v0=" + hmac.new(
        secret.encode("utf-8"),
        sig_basestring,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature_header):
        raise SlackSignatureError("signature mismatch")
