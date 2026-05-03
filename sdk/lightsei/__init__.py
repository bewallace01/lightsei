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
from ._secrets import get_secret as _impl_get_secret
from ._track import track
from .errors import LightseiError, LightseiPolicyError

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
    "get_run_id",
    "get_secret",
    "on_command",
    "on_chat",
    "send_command",
    "claim_command",
    "complete_command",
    "current_dispatch_chain_id",
    "LightseiError",
    "LightseiPolicyError",
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


def emit(
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    run_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> None:
    _client.emit(kind, payload, run_id=run_id, agent_name=agent_name)


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


def send_command(
    target_agent: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    dispatch_chain_id: Optional[str] = None,
    source_agent: Optional[str] = None,
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

    Raises LightseiError on transport or non-2xx.
    """
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
