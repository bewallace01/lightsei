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


class LightseiEscalate(Exception):
    """Phase 21.5: raise from inside an @on_chat("widget") handler to
    escalate the conversation to a human.

    Not a subclass of LightseiError on purpose — this is a control-
    flow signal, not an error. The 21.6 widget orchestrator catches
    it explicitly and POSTs to /widget-bot/escalate on the bot's
    behalf. User code that catches LightseiError must not also
    swallow this exception.

    Use this instead of `lightsei.escalate(conversation_id, reason)`
    when you want the handler to short-circuit cleanly:

        @lightsei.on_chat("widget")
        def handle(turn):
            if "refund" in turn["user_message"].lower():
                raise lightsei.LightseiEscalate(
                    "refund_request",
                    payload={"last_user_message": turn["user_message"]},
                )
            return generate_reply(turn)
    """

    def __init__(
        self,
        reason: str,
        payload: Optional[dict[str, Any]] = None,
    ):
        self.reason = reason
        self.payload = payload or {}
        super().__init__(
            f"escalating widget conversation (reason={reason!r})"
        )


class LightseiConnectorZoneError(LightseiError):
    """Raised when a bot calls an installed connector that refuses
    its trust zone.

    Phase 20.6: each connector declares which sensitivity zones can
    use it (e.g. Gmail's declared_zones excludes 'public', so a
    public-zoned research bot literally cannot send email even if it
    has the connector:gmail capability). Backend returns 403 with
    `connector_zone_mismatch`; the SDK surfaces it as this exception
    so user code can catch the trust-zone violation specifically.

    Attributes:
        connector_type: which connector refused the call.
        agent_name: who tried to call it.
        agent_sensitivity_level: the agent's zone.
        declared_zones: the connector's allow-list.
    """

    def __init__(
        self,
        connector_type: str,
        agent_name: Optional[str],
        agent_sensitivity_level: Optional[str],
        declared_zones: Optional[list[str]] = None,
        message: Optional[str] = None,
    ):
        self.connector_type = connector_type
        self.agent_name = agent_name
        self.agent_sensitivity_level = agent_sensitivity_level
        self.declared_zones = list(declared_zones or [])
        super().__init__(
            message
            or (
                f"connector {connector_type!r} refuses calls from "
                f"{agent_sensitivity_level!r}-zoned bots "
                f"(agent {agent_name!r}). Declared zones: "
                f"{self.declared_zones}."
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
