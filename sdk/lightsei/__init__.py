"""Lightsei Python SDK.

Public surface:
    lightsei.init(api_key, agent_name, version)
    @lightsei.track
    lightsei.emit(kind, payload)
    lightsei.flush()
    lightsei.shutdown()
    lightsei.get_run_id()
"""

import logging
from typing import Any, Optional

from ._chat import on_chat
from ._client import _client
from ._commands import (
    claim_command as _impl_claim_command,
    complete_command as _impl_complete_command,
    current_dispatch_chain_id,
    on_command,
    send_command as _impl_send_command,
)
from ._context import get_run_id
from ._trigger import on_trigger, trigger
from ._cost_insights import get_cost_insights as _impl_get_cost_insights
from ._instance import TooManyInstancesError
from ._quality_signal import get_quality_signal as _impl_get_quality_signal
from ._secrets import get_secret as _impl_get_secret
from ._track import track
from .connectors import gmail, google_calendar as calendar, google_drive as drive
from .errors import (
    LightseiCapabilityError,
    LightseiConnectorZoneError,
    LightseiError,
    LightseiEscalate,
    LightseiPolicyError,
)

_log = logging.getLogger("lightsei")

# Resolved from package metadata at import time so there's a single source
# of truth (pyproject.toml). Falls back to a sentinel when the package is
# imported from a source tree that hasn't been installed (e.g., directly
# from a git clone with `python -c "import lightsei"`); that path isn't
# the normal install flow but shouldn't crash on import.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        __version__ = _pkg_version("lightsei")
    except PackageNotFoundError:
        __version__ = "0.0.0+source"
    finally:
        del _pkg_version
        del PackageNotFoundError
except Exception:  # pragma: no cover — extremely defensive
    __version__ = "0.0.0+unknown"

__all__ = [
    "__version__",
    "init",
    "track",
    "emit",
    "flush",
    "shutdown",
    "check_policy",
    "get_cost_insights",
    "get_quality_signal",
    "get_run_id",
    "handoff_span",
    "redact",
    "register_redactor",
    "get_secret",
    "on_command",
    "on_chat",
    "send_command",
    "claim_command",
    "complete_command",
    "current_dispatch_chain_id",
    "LightseiError",
    "LightseiPolicyError",
    "LightseiCapabilityError",
    "LightseiConnectorZoneError",
    "LightseiEscalate",
    "TooManyInstancesError",
    "gmail",
    "calendar",
    "drive",
    "respond",
    "escalate",
    "on_trigger",
    "trigger",
]


def init(
    api_key: Optional[str] = None,
    agent_name: Optional[str] = None,
    version: str = "0.0.0",
    *,
    base_url: Optional[str] = None,
    flush_interval: Optional[float] = None,
    batch_size: Optional[int] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
    capture_content: Optional[bool] = None,
    command_poll_interval: Optional[float] = None,
    chat_poll_interval: Optional[float] = None,
    heartbeat_interval: Optional[float] = None,
) -> None:
    """Initialize Lightsei. Idempotent: a second call is ignored.

    Set capture_content=False to opt out of recording the messages and
    response text in events. Token counts and metadata are still captured.

    Set command_poll_interval (seconds) to change how often the background
    thread checks the dashboard for pending commands. Default 5 seconds.
    Register handlers with `@lightsei.on_command(kind)` BEFORE calling init().
    """
    _client.init(
        api_key=api_key,
        agent_name=agent_name,
        version=version,
        base_url=base_url,
        flush_interval=flush_interval,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        capture_content=capture_content,
        command_poll_interval=command_poll_interval,
        chat_poll_interval=chat_poll_interval,
        heartbeat_interval=heartbeat_interval,
    )
    _auto_patch()
    # Phase 16.3: pull the agent's capability list once so the gate
    # is active on the user's first outbound call. The httpx patch
    # fails open until this fires (`has_capability` returns True when
    # the cache hasn't loaded yet), so a fast `httpx.get()` immediately
    # after init() still works — the gate engages as soon as the
    # initial fetch returns, then refreshes on every heartbeat
    # response (in _instance.py).
    try:
        from ._capabilities import fetch_capabilities
        fetch_capabilities(_client)
    except Exception as e:
        _log.debug("lightsei capabilities fetch failed: %s", e)


def _auto_patch() -> None:
    try:
        from .integrations.openai_patch import patch_openai
        patch_openai()
    except Exception as e:
        _log.warning("lightsei openai auto-patch failed: %s", e)
    try:
        from .integrations.anthropic_patch import patch_anthropic
        patch_anthropic()
    except Exception as e:
        _log.warning("lightsei anthropic auto-patch failed: %s", e)
    try:
        from .integrations.gemini_patch import patch_gemini
        patch_gemini()
    except Exception as e:
        _log.warning("lightsei gemini auto-patch failed: %s", e)
    # Phase 16.3: trust-zone capability gate on outbound HTTP. Wraps
    # httpx.Client.send + httpx.AsyncClient.send so a bot without
    # 'internet' can't make outbound network calls. SDK's own backend
    # calls bypass via host whitelist (see _capabilities.is_lightsei_
    # internal_url). Idempotent — dev-reload doesn't double-wrap.
    try:
        from .integrations.httpx_patch import patch_httpx
        patch_httpx()
    except Exception as e:
        _log.warning("lightsei httpx auto-patch failed: %s", e)


def emit(
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    run_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    redact: bool = True,
) -> None:
    """Emit a telemetry event.

    Phase 16.5: when the agent's sensitivity_level is `'pii'`, the
    payload is recursively redacted (email / phone / SSN / Luhn-
    valid credit cards replaced with `[redacted-*]` placeholders)
    before it leaves the SDK. Pass `redact=False` to opt out per-call
    when the operator genuinely needs the raw value (e.g. an
    audit-trail bot whose whole job is preserving exact input).
    """
    if (
        redact
        and payload is not None
        and getattr(_client, "_sensitivity_level", None) == "pii"
    ):
        from ._redaction import redact_payload
        payload = redact_payload(payload)
    _client.emit(kind, payload, run_id=run_id, agent_name=agent_name)


def redact(text: str, *, detectors: Optional[list[str]] = None) -> str:
    """Phase 16.5: redact a string with the built-in PII detectors
    (email, US phone, SSN, Luhn-valid credit card) + any custom
    detectors registered via `register_redactor`. Pass `detectors=`
    a list to restrict which detectors run (default: all)."""
    from ._redaction import redact as _impl
    return _impl(text, detectors=detectors)


def register_redactor(name: str, fn: Any) -> None:
    """Phase 16.5: register a custom detector that runs alongside the
    built-ins. Same `name` as a built-in replaces the built-in's
    implementation for that detector. Detector signature: takes a
    string, returns a string with the redacted substrings replaced
    by a placeholder like `[redacted-mything]`."""
    from ._redaction import register_redactor as _impl
    _impl(name, fn)


def handoff_span(
    from_run: str,
    to_run: str,
    sanitized_prompt: str,
    *,
    notes: Optional[str] = None,
) -> None:
    """Phase 16.5: record a human-mediated handoff between two runs.

    Emits a `handoff` event linking `from_run` (the upstream run the
    operator read output from) to `to_run` (the downstream run the
    operator wrote sanitized prompt into). Lets cross-zone chains be
    reassembled in traces even though the actual data didn't cross
    the boundary — the human was the translation layer.

    Opt-in (no auto-detection). The Phase 21 operator chat surface
    will call this when an operator finishes a translation; users
    can call it directly today.

    `sanitized_prompt` is recorded as-is (not re-redacted) since the
    caller already sanitized it by definition. Pass `notes` for any
    free-form context the operator wants to attach (why the
    translation was needed, what they removed, etc.)."""
    payload: dict[str, Any] = {
        "from_run": from_run,
        "to_run": to_run,
        "sanitized_prompt": sanitized_prompt,
    }
    if notes is not None:
        payload["notes"] = notes
    # redact=False — `sanitized_prompt` is already clean by contract;
    # redacting again could double-redact placeholders the operator
    # deliberately typed (e.g. "[redacted-email]" as an explicit
    # placeholder in the prompt).
    _client.emit("handoff", payload)


def check_policy(
    action: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    run_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> dict[str, Any]:
    return _client.check_policy(
        action, payload, run_id=run_id, agent_name=agent_name
    )


def flush(timeout: float = 2.0) -> None:
    _client.flush(timeout=timeout)


def shutdown() -> None:
    _client.shutdown()


def get_secret(name: str, *, ttl_s: Optional[float] = None) -> str:
    """Fetch a workspace secret stored in the dashboard. Cached for 5 minutes
    by default; pass ttl_s=0 to force a refetch.

    Typical use:
        OPENAI_API_KEY = lightsei.get_secret("OPENAI_API_KEY")

    Raises LightseiError if the backend is unreachable or the secret is
    unset — secrets are usually keys, so failing closed is the right default.
    """
    return _impl_get_secret(_client, name, ttl_s=ttl_s)


def get_agent_config(name: str) -> dict[str, Any]:
    """Fetch an agent's pinned `provider` + `model` from the dashboard.

    Returns the dict shape `{"provider": str | None, "model": str | None}`
    — both null when the agent has no pin set, in which case the caller
    should fall back to whatever default it would use without Lightsei
    routing. Bots that branch on provider call this once per tick (cheap
    GET, no caching today) so a dashboard model swap takes effect on the
    very next tick.

    Raises `LightseiError` on transport failure. A 404 (agent not in this
    workspace) returns `{provider: None, model: None}` rather than raise,
    since "no pin" is the right semantics for an unknown agent.
    """
    from .errors import LightseiError

    if _client._http is None:
        raise LightseiError(
            "get_agent_config called before lightsei.init() — "
            "no HTTP client available"
        )
    try:
        r = _client._http.get(
            f"/agents/{name}", timeout=_client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"get_agent_config transport error: {e}") from e
    if r.status_code == 404:
        return {"provider": None, "model": None}
    if r.status_code >= 400:
        raise LightseiError(
            f"get_agent_config failed: {r.status_code} {r.text[:200]}"
        )
    body = r.json() or {}
    return {
        "provider": body.get("provider"),
        "model": body.get("model"),
        # Optional per-agent schedule override for cron-style bots.
        # null when unset; bot reads its env default in that case.
        "tick_interval_s": body.get("tick_interval_s"),
    }


def get_cost_insights() -> list[dict[str, Any]]:
    """Fetch this workspace's cost insights (Phase 12D).

    Returns the homogeneous list of insight dicts (`kind`, `headline`,
    `detail`, `apply`) the dashboard's `/cost/insights` page renders.
    Cron-style bots like Polaris call this on each tick and emit a
    `polaris.cost_analysis` event so the home page can surface the
    audit alongside the latest plan.

    Fails *open*: returns `[]` on any error (unreachable backend,
    non-200, malformed body). Cost insights are enrichment, not
    essential — the bot's tick should never block on this.
    """
    return _impl_get_cost_insights(_client)


def get_quality_signal(
    agent_name: str, *, days: int = 7,
) -> Optional[dict[str, Any]]:
    """Fetch this workspace's quality summary for one agent (Phase 14).

    Returns the dict the dashboard's /agents/{name} Quality section
    renders: `agent_name`, `days`, `verdict_counts`, `total_evaluations`,
    `recent_bads`, `trend`. The verdict source is the eval runner's
    judge LLM (Phase 14.3).

    Fails *closed*: returns `None` on any error (unreachable backend,
    non-200, malformed body, SDK not initialized). Unlike
    `get_cost_insights` — where `[]` is a fine no-op — quality has a
    real "no evals yet" state (zero counts) that's meaningfully
    different from a fetch failure. Returning `None` on failure forces
    the caller to handle the distinction; matters most for
    auto-tuners (Phase 12D.3) that must never tune blindly when the
    signal is unavailable.
    """
    return _impl_get_quality_signal(_client, agent_name, days=days)


def post_slack(
    channel: str,
    text: str,
    *,
    thread_ts: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Phase 19.5: post a message to Slack from inside a bot.

    Used from `@lightsei.on_command("slack.respond")` handlers
    (dispatched by the Phase 19.4 chat orchestrator when a user mentions
    the Lightsei app) to reply back to the channel:

        @lightsei.on_command("slack.respond")
        def on_slack(payload):
            answer = compute_response(payload["text"])
            lightsei.post_slack(
                channel=payload["channel_id"],
                text=answer,
                thread_ts=payload.get("thread_ts"),
            )

    The bot's Slack bot token never leaves the backend — Lightsei
    resolves the workspace's install + makes the chat.postMessage
    call. Bot code only sees the channel id and the response text.

    Raises LightseiCapabilityError if the bot doesn't have the
    `slack:respond` capability granted. Default-deny: Compliance
    preset's internal + public hint mappings grant it automatically;
    pii + sensitive bots need an operator to add it explicitly.

    Returns `{ok, ts, channel}` on success. Raises LightseiError on
    transport failure or non-2xx response from the backend.
    """
    from ._capabilities import check_capability
    from .errors import LightseiError

    if not channel:
        raise ValueError("post_slack requires a channel")
    if not text:
        raise ValueError("post_slack requires a text body")
    if _client is None or _client._http is None:
        raise LightseiError(
            "post_slack called before lightsei.init() — "
            "no HTTP client available"
        )

    # Capability gate. Same fail-open-during-init pattern as
    # send_command: check_capability returns silently when the cache
    # hasn't loaded yet (the initial /agents/{name}/capabilities
    # fetch runs in init() but the first post_slack call might land
    # before the response).
    check_capability(_client, "slack:respond")

    src = source_agent or getattr(_client, "_agent_name", None)
    if not src:
        raise LightseiError(
            "post_slack requires source_agent (set via lightsei.init("
            "agent_name=...) or pass explicitly)"
        )

    body: dict[str, Any] = {
        "source_agent": src,
        "channel": channel,
        "text": text,
    }
    if thread_ts:
        body["thread_ts"] = thread_ts

    try:
        r = _client._http.post(
            "/slack/respond",
            json=body,
            timeout=_client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"post_slack transport error: {e}") from e

    if r.status_code >= 400:
        # 403 capability_missing → typed LightseiCapabilityError so
        # user code can catch it specifically rather than via a
        # status-code probe.
        if r.status_code == 403:
            try:
                body_json = r.json()
            except Exception:
                body_json = {}
            detail = body_json.get("detail") if isinstance(body_json, dict) else None
            if isinstance(detail, dict) and detail.get("error") == "capability_missing":
                from .errors import LightseiCapabilityError
                raise LightseiCapabilityError(
                    capability=str(detail.get("capability") or "slack:respond"),
                    granted=detail.get("granted") or [],
                    agent_name=detail.get("agent_name") or src,
                )
        raise LightseiError(f"post_slack returned {r.status_code}: {r.text[:300]}")

    try:
        return r.json()
    except Exception as e:
        raise LightseiError(f"post_slack returned non-JSON body: {e}") from e


def respond(
    conversation_id: str,
    text: str,
    *,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Phase 21.5: post a bot reply into a widget conversation.

    Used implicitly when an `@on_chat("widget")` handler returns a
    string — the 21.6 orchestrator calls this on the bot's behalf.
    Exposed so a bot can post async follow-ups (e.g. "Looking that
    up…" then later "Here's the answer").

    Raises LightseiCapabilityError if the bot doesn't have the
    `widget:respond` capability. The customer-facing bot gets this
    by default; other bots have to be granted it explicitly via
    `PATCH /agents/{name}/capabilities`.

    Returns `{ok, message_id, conversation_id}` on success. Raises
    LightseiError on transport failure or non-2xx response.
    """
    from ._capabilities import check_capability

    if not conversation_id:
        raise ValueError("respond requires a conversation_id")
    if not text:
        raise ValueError("respond requires a text body")
    if _client is None or _client._http is None:
        raise LightseiError(
            "respond called before lightsei.init() — no HTTP client available"
        )

    check_capability(_client, "widget:respond")

    src = source_agent or _client.agent_name
    if not src:
        raise LightseiError(
            "respond requires source_agent (set via lightsei.init("
            "agent_name=...) or pass explicitly)"
        )

    body: dict[str, Any] = {
        "source_agent": src,
        "conversation_id": conversation_id,
        "text": text,
    }

    try:
        r = _client._http.post(
            "/widget-bot/respond", json=body, timeout=_client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"respond transport error: {e}") from e

    if r.status_code >= 400:
        # 403 capability_missing → typed LightseiCapabilityError.
        # 409 conversation_resolved → plain LightseiError with the
        # error code in the message so caller code can string-match.
        if r.status_code == 403:
            try:
                body_json = r.json()
            except Exception:
                body_json = {}
            detail = body_json.get("detail") if isinstance(body_json, dict) else None
            if isinstance(detail, dict) and detail.get("error") == "capability_missing":
                from .errors import LightseiCapabilityError
                raise LightseiCapabilityError(
                    capability=str(detail.get("capability") or "widget:respond"),
                    granted=detail.get("granted") or [],
                    agent_name=detail.get("agent_name") or src,
                )
        raise LightseiError(f"respond returned {r.status_code}: {r.text[:300]}")

    try:
        return r.json()
    except Exception as e:
        raise LightseiError(f"respond returned non-JSON body: {e}") from e


def escalate(
    conversation_id: str,
    reason: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Phase 21.5: flip a widget conversation to escalated.

    Creates a `widget_escalations` row, marks the conversation
    status `escalated`, drops a system message into the thread
    ("This conversation has been handed off to a human."), and
    surfaces the escalation in the operator inbox (Phase 21.8).

    Two equivalent ways for the bot to escalate from inside an
    `@on_chat("widget")` handler:

        # Imperative — useful when you want the handler to
        # continue running after escalating.
        lightsei.escalate(turn["conversation_id"], "refund_request")

        # Exception-driven — cleaner when escalating short-circuits
        # the handler.
        raise lightsei.LightseiEscalate("refund_request",
                                        payload={"hint": "..."})

    Raises LightseiCapabilityError if the bot doesn't have
    `widget:escalate`. Idempotent on already-escalated /
    operator_owned / resolved conversations (the backend
    short-circuits and returns `noop: true`).
    """
    from ._capabilities import check_capability

    if not conversation_id:
        raise ValueError("escalate requires a conversation_id")
    if not reason:
        raise ValueError("escalate requires a reason")
    if _client is None or _client._http is None:
        raise LightseiError(
            "escalate called before lightsei.init() — no HTTP client available"
        )

    check_capability(_client, "widget:escalate")

    src = source_agent or _client.agent_name
    if not src:
        raise LightseiError(
            "escalate requires source_agent (set via lightsei.init("
            "agent_name=...) or pass explicitly)"
        )

    body: dict[str, Any] = {
        "source_agent": src,
        "conversation_id": conversation_id,
        "reason": reason,
        "payload": payload or {},
    }

    try:
        r = _client._http.post(
            "/widget-bot/escalate", json=body, timeout=_client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"escalate transport error: {e}") from e

    if r.status_code >= 400:
        if r.status_code == 403:
            try:
                body_json = r.json()
            except Exception:
                body_json = {}
            detail = body_json.get("detail") if isinstance(body_json, dict) else None
            if isinstance(detail, dict) and detail.get("error") == "capability_missing":
                from .errors import LightseiCapabilityError
                raise LightseiCapabilityError(
                    capability=str(detail.get("capability") or "widget:escalate"),
                    granted=detail.get("granted") or [],
                    agent_name=detail.get("agent_name") or src,
                )
        raise LightseiError(f"escalate returned {r.status_code}: {r.text[:300]}")

    try:
        return r.json()
    except Exception as e:
        raise LightseiError(f"escalate returned non-JSON body: {e}") from e


def send_command(
    target_agent: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    dispatch_chain_id: Optional[str] = None,
    source_agent: Optional[str] = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Enqueue a command for another agent. Returns the created command.

    Typical use, from inside an `@on_command` handler or a `claim_command`
    block:

        @lightsei.on_command("polaris.evaluate_push")
        def on_push(payload):
            cmd = lightsei.send_command(
                "atlas",
                "atlas.run_tests",
                {"commit": payload["commit"]},
            )
            return {"dispatched": cmd["id"]}

    The dispatch chain id is inherited from the active claim's thread-local
    context if present, otherwise generated fresh. Pass `dispatch_chain_id`
    explicitly to override (rare; only useful for tests or for joining a
    chain id from outside the SDK's normal flow).

    Phase 16.5: when the source agent's sensitivity_level is `'pii'`,
    the dispatched payload is recursively redacted before it leaves
    the SDK. Pass `redact=False` to opt out per-call. The capability
    gate (Phase 16.3) + cross-zone gate (Phase 16.4) run regardless.

    Raises LightseiError on transport or non-2xx. Raises
    LightseiCapabilityError if `'send_command'` isn't granted.
    Raises LightseiCrossZoneError if target's zone differs from
    source's and the source isn't opted into cross-zone dispatch.
    """
    if (
        redact
        and payload is not None
        and getattr(_client, "_sensitivity_level", None) == "pii"
    ):
        from ._redaction import redact_payload
        payload = redact_payload(payload)
    return _impl_send_command(
        _client,
        target_agent,
        kind,
        payload,
        dispatch_chain_id=dispatch_chain_id,
        source_agent=source_agent,
    )


def claim_command(
    *, agent_name: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending command for this agent.

    Returns the command dict, or None when the queue is empty. Use this for
    explicit polling control; the `@on_command` decorator + auto-poller
    works for the common case where one handler per kind is enough.

    Sets the per-thread dispatch context so subsequent `send_command` calls
    inherit the chain id automatically. The context clears on
    `complete_command`.
    """
    return _impl_claim_command(_client, agent_name=agent_name)


def complete_command(
    command_id: str,
    *,
    result: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Mark a claimed command done (success) or failed (error). Clears this
    thread's dispatch context. Pass exactly one of `result` or `error`."""
    return _impl_complete_command(
        _client, command_id, result=result, error=error
    )
