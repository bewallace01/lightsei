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
from ._commands import on_command
from ._context import get_run_id
from ._track import track
from .errors import LightseiError, LightseiPolicyError

_log = logging.getLogger("lightsei")

__all__ = [
    "init",
    "track",
    "emit",
    "flush",
    "shutdown",
    "check_policy",
    "get_run_id",
    "on_command",
    "on_chat",
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
