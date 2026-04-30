"""Mattermost — Slack-compatible incoming webhook.

Mattermost accepts the Slack incoming-webhook payload format
verbatim, so the format function is `slack.format`. The dispatcher's
REGISTRY entry for "mattermost" wires Slack's formatter directly;
this module exists so the channel-type label "mattermost" has its
own home and we have a place to put Mattermost-specific tweaks
(e.g., username overrides, channel overrides) if we ever need them.

The post() implementation is identical to Slack's — JSON POST, no
secret. Lives here rather than aliasing slack.post to keep the
registry's `(format_fn, post_fn)` pair self-contained per type and
make stack traces blame the right module.
"""
from typing import Any

from ._http import post_json
from ._types import Delivery


def post(*, url: str, body: dict[str, Any], secret_token: str | None = None) -> Delivery:
    del secret_token  # explicit: documented unused
    return post_json(url=url, body=body)
