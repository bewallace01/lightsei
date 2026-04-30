"""Outbound-notification dispatcher (Phase 9.2).

Mirrors the shape of `backend/validators/`: a registry of per-platform
modules, each exporting `format(signal, *, trigger)` and `post(url,
body, *, secret_token=None) -> Delivery`. The public `dispatch(channel,
signal)` picks the right pair, runs the format → post pipeline, and
returns a `Delivery` audit record.

Per-formatter modules:
  slack.py         Block Kit JSON
  discord.py       Embed objects with color coding
  teams.py         Adaptive Card 1.5 (modern Workflows webhook URL)
  mattermost.py    Slack-compat alias — re-uses Slack's formatter
  webhook.py       Generic Lightsei JSON envelope (Phase 9.3)

Phase 9.4 wires the dispatcher into POST /events so registered channels
fire automatically on the right triggers. Phase 9.1's test-fire stub
gets swapped to use this dispatcher in 9.2.

Pure-function constraint per formatter: no DB writes, no shared state.
The dispatcher itself does I/O (HTTP-out) but does NOT raise on
failure — every path produces a `Delivery` row that the caller
persists.
"""
from typing import Any, Callable

from . import discord, mattermost, slack, teams, webhook
from ._types import Delivery, Signal


# `format` returns the platform-native HTTP body (a dict that gets
# serialized to JSON before posting). `post` performs the HTTP-out and
# returns a Delivery. Splitting them lets tests assert on the formatted
# shape without making real HTTP calls, and lets the test-fire endpoint
# share one code path with the trigger pipeline.
FormatFn = Callable[[Signal], dict[str, Any]]
PostFn = Callable[..., Delivery]


REGISTRY: dict[str, tuple[FormatFn, PostFn]] = {
    "slack":      (slack.format,      slack.post),
    "discord":    (discord.format,    discord.post),
    "teams":      (teams.format,      teams.post),
    # Mattermost accepts Slack incoming-webhook JSON verbatim. Re-using
    # Slack's formatter (rather than importing slack.format from a
    # mattermost.py wrapper) keeps the registry honest about the
    # actual code path. The dashboard still labels rows as "mattermost"
    # because the type field on the channel row is what's stored.
    "mattermost": (slack.format,      mattermost.post),
    # Generic webhook with the Lightsei JSON envelope + optional HMAC
    # signing — the integration path for anything not natively
    # supported (n8n, Zapier, custom services). Phase 9.3.
    "webhook":    (webhook.format,    webhook.post),
}


def dispatch(*, channel_type: str, target_url: str, signal: Signal,
             secret_token: str | None = None) -> Delivery:
    """Run a signal through the right formatter + poster.

    Never raises — unknown channel types and post-time exceptions
    produce a failed `Delivery` rather than letting the caller's
    transaction blow up. The caller writes the returned Delivery to
    `notification_deliveries` regardless of status.
    """
    pair = REGISTRY.get(channel_type)
    if pair is None:
        return Delivery(
            status="failed",
            response_summary={
                "error": "unknown_channel_type",
                "message": (
                    f"channel type {channel_type!r} is not registered; "
                    f"known: {sorted(REGISTRY)}"
                ),
            },
        )
    format_fn, post_fn = pair
    try:
        body = format_fn(signal)
    except Exception as exc:
        return Delivery(
            status="failed",
            response_summary={
                "error": "formatter_exception",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
    return post_fn(url=target_url, body=body, secret_token=secret_token)


__all__ = ["Delivery", "REGISTRY", "Signal", "dispatch"]
