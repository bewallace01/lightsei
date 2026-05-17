"""Phase 16.3: client-side capability cache + gate.

The SDK fetches the agent's capability allow-list on `lightsei.init()`
(via a GET on /agents/{name}) and refreshes it on every heartbeat
response (Phase 16.2's heartbeat endpoint carries the list back so
dashboard edits propagate within a tick). `check_capability(name)`
raises `LightseiCapabilityError` if the capability isn't on the
list; httpx + send_command wrappers call it before the actual op.

Fail-open before init(): a bot that imports httpx and does
`httpx.get(...)` BEFORE calling `lightsei.init()` is not gated.
Matches CLAUDE.md's graceful-degradation principle — the user opts
into the gate by calling init(). Post-init, the gate is active.

Fail-open on fetch error: if the SDK can't reach the backend to fetch
the initial capability list, the gate stays disabled rather than
breaking the bot's startup. Logged at debug. Heartbeat refreshes
will pick up the list once the backend is reachable.

Whitelist for the Lightsei backend itself: the SDK's own HTTP calls
(event posts, heartbeats, secret fetches, etc.) MUST bypass the
gate, otherwise the SDK can't function without 'internet'. The
httpx_patch checks `is_lightsei_internal_url(client, url)` before
gating — no whitelist of the user's own connector hosts (Phase 20
will gate those via the connector: prefix).
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

from .errors import LightseiCapabilityError

logger = logging.getLogger("lightsei.capabilities")


def update_capabilities(client, capabilities: Optional[list[str]]) -> None:
    """Replace the SDK's cached allow-list. Called from init() (initial
    fetch) and from each heartbeat response. `None` is treated as
    'capabilities not known yet' — the gate stays disabled until we
    successfully fetch one."""
    if capabilities is None:
        return
    if not isinstance(capabilities, list):
        logger.debug(
            "lightsei capabilities: ignoring non-list payload %r",
            type(capabilities).__name__,
        )
        return
    # Defensive copy + str-cast in case a future server sends something
    # exotic in the list. Anything that's not a string gets dropped.
    cleaned = [c for c in capabilities if isinstance(c, str)]
    client._capabilities_cache = cleaned
    client._capabilities_loaded = True


def has_capability(client, name: str) -> bool:
    """Pure check. Returns True when the gate is disabled (no cache
    loaded yet) so the SDK fails open before init() completes."""
    if not getattr(client, "_capabilities_loaded", False):
        return True
    return name in getattr(client, "_capabilities_cache", [])


def check_capability(client, name: str) -> None:
    """Raise LightseiCapabilityError if `name` isn't granted.

    Caller is the SDK wrapper (httpx_patch.send, send_command). The
    cache is the source of truth; backend re-checks anyway as
    defense-in-depth.
    """
    if has_capability(client, name):
        return
    raise LightseiCapabilityError(
        capability=name,
        granted=getattr(client, "_capabilities_cache", []),
        agent_name=getattr(client, "agent_name", None),
    )


def fetch_capabilities(client) -> None:
    """Initial fetch on init(). Reads from /agents/{name} and stuffs
    the response's `capabilities` field into the cache. Fails open on
    any error — startup must not crash because the backend's slow."""
    if client._http is None or not client.agent_name:
        return
    try:
        r = client._http.get(
            f"/agents/{client.agent_name}",
            timeout=client.timeout,
        )
    except Exception as e:
        logger.debug("lightsei capabilities: initial fetch failed: %s", e)
        return
    if r.status_code != 200:
        logger.debug(
            "lightsei capabilities: GET /agents/%s returned %s",
            client.agent_name, r.status_code,
        )
        return
    try:
        body = r.json()
    except Exception as e:
        logger.debug("lightsei capabilities: malformed agent response: %s", e)
        return
    if isinstance(body, dict):
        update_capabilities(client, body.get("capabilities"))


def is_lightsei_internal_url(client, url: object) -> bool:
    """Return True when `url` targets the Lightsei backend itself —
    the SDK's own HTTP calls must bypass the capability gate or the
    bot can't post events / heartbeats / fetch secrets.

    Accepts a URL string or anything with a `.host` attribute
    (httpx.URL). Robust against odd inputs (returns False rather than
    raising)."""
    base = getattr(client, "base_url", None)
    if not base:
        return False
    try:
        base_host = urlparse(base).hostname
    except Exception:
        return False
    if not base_host:
        return False
    target_host: Optional[str] = None
    if isinstance(url, str):
        try:
            target_host = urlparse(url).hostname
        except Exception:
            target_host = None
    else:
        # httpx.URL has a `.host` attribute. Try it before bailing.
        target_host = getattr(url, "host", None)
    return bool(target_host) and target_host == base_host