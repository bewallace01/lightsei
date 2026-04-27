"""Auth for the Phase 5.2 worker-facing endpoints.

The worker is a system component — it pulls deployments from any workspace
and injects every workspace's secrets as env vars into the bot it spawns.
This is fundamentally different from user auth (which is workspace-scoped).
A leaked worker token grants cross-tenant access; treat it like the
LIGHTSEI_SECRETS_KEY in operational sensitivity.

Set `LIGHTSEI_WORKER_TOKEN` (any random string, ~32 bytes urlsafe) on the
backend service and on the worker. If unset, every /worker/* endpoint
returns 503 — fail closed, by design.
"""
import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException


def _expected_token() -> Optional[str]:
    return os.environ.get("LIGHTSEI_WORKER_TOKEN") or None


def _parse_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def get_worker(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dep that authenticates a request as coming from the worker.
    Returns None on success; raises HTTPException otherwise."""
    expected = _expected_token()
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail="worker auth unavailable: LIGHTSEI_WORKER_TOKEN is not configured",
        )
    presented = _parse_bearer(authorization)
    if presented is None:
        raise HTTPException(status_code=401, detail="missing worker token")
    # Constant-time compare to discourage timing attacks against a long-lived
    # shared secret.
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid worker token")
    return None
