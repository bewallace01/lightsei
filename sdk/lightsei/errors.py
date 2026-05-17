from typing import Any, Optional


class LightseiError(Exception):
    """Base class for Lightsei-raised errors."""


class LightseiPolicyError(LightseiError):
    """Raised when a policy check denies an action."""

    def __init__(self, reason: str, decision: Optional[dict[str, Any]] = None):
        self.reason = reason
        self.decision = decision or {}
        super().__init__(reason)


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
