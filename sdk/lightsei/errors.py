from typing import Any, Optional


class LightseiError(Exception):
    """Base class for Lightsei-raised errors."""


class LightseiPolicyError(LightseiError):
    """Raised when a policy check denies an action."""

    def __init__(self, reason: str, decision: Optional[dict[str, Any]] = None):
        self.reason = reason
        self.decision = decision or {}
        super().__init__(reason)


class LightseiCrossZoneError(LightseiError):
    """Raised when a dispatch crosses sensitivity zones without the
    source agent having `dispatches_cross_zone=True`.

    Phase 16.4: the load-bearing piece that makes the trust-zone
    boundary actually one-way. A `'pii'` bot can't dispatch to a
    `'public'` bot just because both have `'send_command'` — the
    framework refuses the call. Backend returns 403 with the
    `cross_zone_blocked` error code; the SDK surfaces it as this
    exception so user code can catch the trust-zone violation
    specifically rather than a generic LightseiError.

    Attributes:
        source_agent: who tried to dispatch.
        source_zone: source agent's sensitivity_level.
        target_agent: dispatch target.
        target_zone: target's sensitivity_level.
    """

    def __init__(
        self,
        source_agent: Optional[str],
        source_zone: Optional[str],
        target_agent: Optional[str],
        target_zone: Optional[str],
        message: Optional[str] = None,
    ):
        self.source_agent = source_agent
        self.source_zone = source_zone
        self.target_agent = target_agent
        self.target_zone = target_zone
        super().__init__(
            message
            or (
                f"cross-zone dispatch refused: "
                f"{source_agent!r} ({source_zone!r}) → "
                f"{target_agent!r} ({target_zone!r}). "
                "Set dispatches_cross_zone=True on the source agent to allow."
            ),
        )


class LightseiCapabilityError(LightseiError):
    """Raised when an op requires a capability the agent doesn't have.

    Phase 16.3: the SDK gates outbound ops on the agent's capability
    allow-list (set per-agent on the backend, fetched on init() and
    refreshed on each heartbeat). A call to `httpx.get(...)` from a
    bot without `'internet'` raises this BEFORE the network call
    leaves the process. Same for `lightsei.send_command(...)` without
    `'send_command'`.

    `capability` is the missing capability name; `granted` is the
    agent's current allow-list so the error message can guide the user
    toward fixing it ("add it on /agents/{name}/capabilities").
    """

    def __init__(
        self,
        capability: str,
        granted: Optional[list[str]] = None,
        agent_name: Optional[str] = None,
    ):
        self.capability = capability
        self.granted = list(granted or [])
        self.agent_name = agent_name
        bits = [f"capability {capability!r} not granted"]
        if agent_name:
            bits.append(f"to agent {agent_name!r}")
        bits.append(
            f"(granted: {self.granted or 'none — default-deny'}). "
            "Add it via PATCH /agents/{name}/capabilities or in the "
            "dashboard's agent detail page."
        )
        super().__init__(" ".join(bits))
