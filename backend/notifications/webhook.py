"""Generic webhook formatter — Lightsei-defined JSON envelope.

The integration path for anything not natively supported (n8n,
Zapier, internal job queues, custom services). Receivers consume a
stable shape:

    {
      "type": "polaris.plan" | "validation.fail" | "run_failed" | "test",
      "workspace_id": str,
      "agent_name": str,
      "timestamp": iso8601,             # when the source signal occurred
      "dashboard_url": str,             # deep link a human can click
      "data": { event-specific fields }
    }

When `secret_token` is set on the channel, every request carries:

    X-Lightsei-Signature: sha256=<hex>
    X-Lightsei-Timestamp: <unix epoch seconds>

Verification (receiver side):
    1. Read X-Lightsei-Timestamp; reject if outside the replay window
       (recommend 5 min of clock skew tolerance).
    2. Build signing input = f"{timestamp}.".encode() + raw_body_bytes.
    3. expected = hmac.new(secret, signing_input, sha256).hexdigest().
    4. Constant-time compare with the hex part of X-Lightsei-Signature.

The signing input prefixes the timestamp specifically so a captured
request can't be replayed at a later time even with a valid old
signature — the receiver enforces freshness against its own clock.
"""
import hashlib
import hmac
import json
import time
from typing import Any

from ._http import post_raw
from ._types import Delivery, Signal

# Receivers should ignore requests whose timestamp is outside this
# window relative to their own clock. We don't enforce it on the
# sending side (we always send "now"), but it's part of the contract
# and gets documented in the dashboard's webhook hint.
RECOMMENDED_REPLAY_WINDOW_S = 300


def format(signal: Signal) -> dict[str, Any]:
    return {
        "type": signal.trigger,
        "workspace_id": signal.workspace_id,
        "agent_name": signal.agent_name,
        "timestamp": signal.timestamp.isoformat(),
        "dashboard_url": signal.dashboard_url,
        "data": _data_for(signal),
    }


def post(*, url: str, body: dict[str, Any], secret_token: str | None = None) -> Delivery:
    """Post the envelope. Adds HMAC signing headers when secret_token
    is set."""
    # Deterministic serialization: receivers re-derive the signing
    # input from `f"{ts}.".encode() + raw_body_bytes`, so the bytes we
    # post must be the bytes we sign. sort_keys + compact separators
    # eliminate any nondeterminism httpx's default JSON dumper might
    # introduce in the future.
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if secret_token:
        ts = str(int(time.time()))
        signing_input = f"{ts}.".encode("utf-8") + body_bytes
        digest = hmac.new(
            secret_token.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Lightsei-Timestamp"] = ts
        headers["X-Lightsei-Signature"] = f"sha256={digest}"

    return post_raw(url=url, content=body_bytes, headers=headers)


# ---------- per-trigger data shapes ---------- #


def _data_for(signal: Signal) -> dict[str, Any]:
    """Build the trigger-specific `data` payload.

    Receivers consume this programmatically (not for human display),
    so we ship the full structured fields rather than the truncated
    versions the chat formatters use.
    """
    trigger = signal.trigger
    payload = signal.payload or {}

    if trigger == "polaris.plan":
        return {
            "summary": payload.get("summary"),
            "next_actions": payload.get("next_actions") or [],
            "parking_lot_promotions": payload.get("parking_lot_promotions") or [],
            "drift": payload.get("drift") or [],
            "doc_hashes": payload.get("doc_hashes"),
            "model": payload.get("model"),
            "tokens_in": payload.get("tokens_in"),
            "tokens_out": payload.get("tokens_out"),
        }

    if trigger == "validation.fail":
        # Pass the validations array through as-is. Receivers that
        # only care about failures can filter on `status`.
        return {
            "validations": payload.get("validations") or [],
        }

    if trigger == "run_failed":
        return {
            "error": payload.get("error") or payload.get("error_message") or "",
            "run_id": payload.get("run_id"),
        }

    if trigger == "test":
        return {
            "note": (
                "This is a test message from Lightsei. If you're seeing "
                "this, your webhook is wired up. The HMAC headers are "
                "live too — verify them with your shared secret."
            ),
        }

    # Unrecognized trigger — pass the payload through verbatim so a
    # future trigger doesn't go silent on already-deployed channels.
    return dict(payload)
