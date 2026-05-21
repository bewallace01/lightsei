"""Phase 20.7: shared invoke helper for the connector SDK wrappers.

Each per-connector submodule (lightsei.gmail / lightsei.calendar /
lightsei.drive) has typed top-level functions that funnel through
`_invoke(connector_type, tool_name, payload, source_agent)` here.

The helper handles:

1. Local capability check — raises LightseiCapabilityError before any
   HTTP if the agent's allow-list doesn't include
   `connector:{connector_type}`. Saves a round-trip when the bot is
   obviously misconfigured.
2. source_agent resolution — explicit kwarg wins over the agent_name
   set in `lightsei.init(agent_name=...)`.
3. POST to /connectors/{type}/{tool} on the configured Lightsei
   backend.
4. Error mapping:
   - 403 capability_missing → LightseiCapabilityError (raised on the
     remote side when the local cache is stale)
   - 403 connector_zone_mismatch → LightseiConnectorZoneError
   - 400 connector_not_installed / other 4xx → LightseiError with a
     readable message + the error code
   - 5xx / transport error → LightseiError
5. Success → returns the `result` field (so wrapper functions get
   the unwrapped upstream payload).

Bot code never imports this module directly; it sees the per-tool
typed wrappers and the typed exceptions.
"""
from __future__ import annotations

from typing import Any, Optional

from ._capabilities import check_capability
from ._client import _client
from .errors import (
    LightseiCapabilityError,
    LightseiConnectorZoneError,
    LightseiError,
)


def _invoke(
    *,
    connector_type: str,
    tool_name: str,
    payload: dict[str, Any],
    source_agent: Optional[str],
) -> Any:
    """Internal: drive the bot-callable connector endpoint and unwrap
    the response. Returns whatever the connector's INVOKE function
    returned (the backend nests it under `result`, this strips the
    wrapper)."""

    if _client is None or _client._http is None:
        raise LightseiError(
            f"lightsei.{connector_type} called before lightsei.init() — "
            "no HTTP client available"
        )

    required_capability = f"connector:{connector_type}"

    # Local cache check. Cheap and fails fast when the bot is missing
    # the capability — saves a backend round-trip. The remote endpoint
    # still enforces, so a stale cache can't grant access; this is
    # only a UX shortcut.
    check_capability(_client, required_capability)

    src = source_agent or _client.agent_name
    if not src:
        raise LightseiError(
            f"lightsei.{connector_type}.{tool_name} requires source_agent "
            "(set via lightsei.init(agent_name=...) or pass explicitly)"
        )

    body = {
        "source_agent": src,
        "payload": payload or {},
    }

    try:
        r = _client._http.post(
            f"/connectors/{connector_type}/{tool_name}",
            json=body,
            timeout=_client.timeout,
        )
    except Exception as exc:
        raise LightseiError(
            f"connector {connector_type}.{tool_name} transport error: {exc}"
        ) from exc

    if r.status_code >= 400:
        _raise_typed_error(connector_type, tool_name, src, r)

    try:
        response = r.json()
    except Exception as exc:
        raise LightseiError(
            f"connector {connector_type}.{tool_name} returned non-JSON body"
        ) from exc

    # Backend nests the upstream result under `result`. Wrapper
    # functions consume the unwrapped value so per-tool signatures
    # don't have to deal with the envelope.
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return response


def _raise_typed_error(
    connector_type: str,
    tool_name: str,
    source_agent: str,
    response,
) -> None:
    """Map the backend's 4xx/5xx shape to the right SDK exception
    class. Always raises — this function never returns."""
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, dict):
        detail = {}
    error_code = detail.get("error")
    message_from_backend = detail.get("message") or ""

    if response.status_code == 403 and error_code == "capability_missing":
        raise LightseiCapabilityError(
            capability=str(detail.get("capability") or f"connector:{connector_type}"),
            granted=detail.get("granted") or [],
            agent_name=detail.get("agent_name") or source_agent,
        )

    if response.status_code == 403 and error_code == "connector_zone_mismatch":
        raise LightseiConnectorZoneError(
            connector_type=str(detail.get("connector_type") or connector_type),
            agent_name=detail.get("agent_name") or source_agent,
            agent_sensitivity_level=detail.get("agent_sensitivity_level"),
            declared_zones=detail.get("declared_zones") or [],
        )

    # Everything else (400 connector_not_installed, 404, 502
    # connector_call_failed / connector_auth_failed, 5xx without a
    # detail body) — surface as the base LightseiError with the code
    # in the message so user code can string-match if it needs to.
    suffix = f" ({error_code})" if error_code else ""
    msg = message_from_backend or response.text[:200] or response.reason_phrase
    raise LightseiError(
        f"connector {connector_type}.{tool_name} failed: "
        f"{response.status_code}{suffix}: {msg}"
    )
