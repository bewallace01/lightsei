"""Workspace secrets fetcher.

Calls GET /workspaces/me/secrets/{name} with the configured api_key. Caches
the decrypted value in process memory for a short TTL so a hot path can
poll without hammering the backend.

Fails *closed*: a network error or 4xx raises LightseiError. Secrets are
typically API keys — silently returning None (fail-open) would mask a real
configuration problem until much later when the dependent call breaks.
"""
import logging
import threading
import time
from typing import Optional

from .errors import LightseiError

logger = logging.getLogger("lightsei.secrets")

_DEFAULT_TTL_S = 300.0  # 5 minutes

_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _reset_cache_for_tests() -> None:
    with _lock:
        _cache.clear()


def get_secret(client, name: str, *, ttl_s: Optional[float] = None) -> str:
    """Fetch a workspace secret by name. Returns the decrypted string value.

    Cached in process memory; pass ttl_s=0 to bypass the cache for one call.
    """
    ttl = _DEFAULT_TTL_S if ttl_s is None else ttl_s
    now = time.monotonic()

    if ttl > 0:
        with _lock:
            hit = _cache.get(name)
            if hit is not None and (now - hit[1]) < ttl:
                return hit[0]

    if not client.is_initialized() or client._http is None:
        raise LightseiError(
            "lightsei.get_secret() requires lightsei.init() first"
        )

    try:
        r = client._http.get(
            f"/workspaces/me/secrets/{name}",
            timeout=client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"failed to reach Lightsei backend for secret {name!r}: {e}") from e

    if r.status_code == 404:
        raise LightseiError(f"secret {name!r} is not set in this workspace")
    if r.status_code == 503:
        raise LightseiError(
            f"secrets store unavailable on the backend; ask an operator to set "
            f"LIGHTSEI_SECRETS_KEY (got 503 fetching {name!r})"
        )
    if r.status_code != 200:
        raise LightseiError(
            f"backend returned {r.status_code} for secret {name!r}: {r.text[:200]}"
        )

    try:
        value = r.json()["value"]
    except Exception as e:
        raise LightseiError(f"malformed response for secret {name!r}: {e}") from e

    with _lock:
        _cache[name] = (value, now)
    return value
