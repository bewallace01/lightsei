"""Phase 16.3: httpx capability gate.

Patches `httpx.Client.send` and `httpx.AsyncClient.send` so any
outbound HTTP from a bot is gated on the `'internet'` capability.
Calls to the Lightsei backend itself (event posts, heartbeats,
secret fetches, etc.) bypass the gate via `is_lightsei_internal_url`
— otherwise the SDK couldn't function without `'internet'`, which
would make trust-zone configuration useless for any bot doing real
work.

Idempotent: re-running `patch_httpx()` won't double-wrap. Matches
the `openai_patch.py` / `anthropic_patch.py` pattern so the
auto-patch loop in `__init__.py` can call all three identically.
"""
from __future__ import annotations

import logging
from typing import Any

from .._capabilities import (
    check_capability,
    has_capability,
    is_lightsei_internal_url,
)
from .._client import _client

logger = logging.getLogger("lightsei.httpx_patch")

_INTERNET_CAPABILITY = "internet"
# Sentinel attribute on the patched callables so we don't wrap twice
# if `patch_httpx` runs more than once (dev-reload, multiple `init`s).
_PATCH_MARKER = "_lightsei_capability_patched"


def _should_gate(request: Any) -> bool:
    """Decide whether this httpx request should be capability-gated.

    Gate-skip cases:
      - SDK not initialized yet (fail-open before init).
      - Internet already granted to this agent.
      - Request targets the Lightsei backend itself.

    Otherwise: gate it.
    """
    if not _client.is_initialized():
        return False
    if has_capability(_client, _INTERNET_CAPABILITY):
        return False
    url = getattr(request, "url", None)
    if is_lightsei_internal_url(_client, url):
        return False
    return True


def patch_httpx() -> None:
    """Wrap httpx.Client.send + httpx.AsyncClient.send. Idempotent;
    safe to call from init() repeatedly."""
    try:
        import httpx
    except ImportError:
        logger.debug("lightsei httpx_patch: httpx not installed, skipping")
        return

    _patch_sync_client(httpx)
    _patch_async_client(httpx)


def _patch_sync_client(httpx_mod: Any) -> None:
    original = httpx_mod.Client.send
    if getattr(original, _PATCH_MARKER, False):
        return

    def wrapper(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_gate(request):
            # check_capability raises LightseiCapabilityError; user
            # code sees it bubble up from httpx.get / post / etc.
            # before any bytes leave the process.
            check_capability(_client, _INTERNET_CAPABILITY)
        return original(self, request, *args, **kwargs)

    setattr(wrapper, _PATCH_MARKER, True)
    httpx_mod.Client.send = wrapper  # type: ignore[assignment]


def _patch_async_client(httpx_mod: Any) -> None:
    original = httpx_mod.AsyncClient.send
    if getattr(original, _PATCH_MARKER, False):
        return

    async def wrapper(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_gate(request):
            check_capability(_client, _INTERNET_CAPABILITY)
        return await original(self, request, *args, **kwargs)

    setattr(wrapper, _PATCH_MARKER, True)
    httpx_mod.AsyncClient.send = wrapper  # type: ignore[assignment]
