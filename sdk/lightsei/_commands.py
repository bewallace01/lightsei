"""Agent control plane: receive commands from the Lightsei dashboard, and
dispatch commands to other agents.

Two surfaces, both backed by the same `/agents/{name}/commands` HTTP endpoints:

  1. Decorator + auto-poller (Phase 6):
        @lightsei.on_command("ping")
        def handle_ping(payload):
            return {"pong": True}

     A daemon thread asks the backend for pending commands every
     `command_poll_interval` seconds. For each command the poller looks up
     the matching handler, calls it with the payload, and posts the return
     value (or any raised exception) back as the result. A built-in "ping"
     handler is registered by default so users can verify connectivity from
     the dashboard without writing any code.

  2. Explicit dispatch (Phase 11):
        cmd = lightsei.send_command("atlas", "atlas.run_tests", {...})
        cmd = lightsei.claim_command()
        lightsei.complete_command(cmd["id"], result={"ok": True})

     For agents that want active dispatch (Polaris commanding Atlas), or that
     want explicit claim/run/complete control instead of the auto-poller's
     decorator pattern.

Both surfaces participate in dispatch chains. When `claim_command` claims a
command (whether from the auto-poller's `_dispatch` or a user's explicit
`claim_command()`), the command's `dispatch_chain_id` is stored on a
thread-local. Any `send_command` calls made during the same thread inherit
that chain id automatically — so a chain like `polaris → atlas → hermes`
shows up under one chain id in the dashboard's `/dispatch` view without the
user having to thread the id through call signatures.

Forward-compatibility note: until Phase 11.2 lands the `dispatch_chain_id`
column on the backend's `commands` table, the chain id is generated client-
side and sent as an extra field on the enqueue body. Pydantic on the server
silently drops unknown fields, so it's a no-op there for now; persistence +
depth caps + approval state arrive in 11.2.
"""
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("lightsei.commands")

# kind -> callable(payload: dict) -> dict | None
_handlers: Dict[str, Callable[[dict[str, Any]], Optional[dict[str, Any]]]] = {}
# kind -> human-readable description (for the dashboard's manifest)
_descriptions: Dict[str, str] = {}

# Per-thread dispatch context. Set on `claim_command` (and the auto-poller's
# `_dispatch`) so subsequent `send_command` calls in the same thread inherit
# the chain id without the caller having to thread it through. Cleared on
# `complete_command` and at the end of each `_dispatch` call.
_command_context = threading.local()


def on_command(kind: str, *, description: Optional[str] = None):
    """Decorator: register a handler for a command kind.

    The handler receives the command's payload dict. Its return value (must
    be a dict or None) becomes the command's result. If the handler raises,
    the command is marked failed with the exception's repr.

    `description` is optional but recommended — it's published to the
    dashboard's send-command dropdown so anyone clicking around can see what
    each kind does without reading source.
    """
    def decorator(fn: Callable[[dict[str, Any]], Optional[dict[str, Any]]]):
        _handlers[kind] = fn
        if description:
            _descriptions[kind] = description
        return fn
    return decorator


# Built-in: a simple ping/pong so the dashboard's "send command" form is
# useful out of the box, even before the user writes any handlers.
@on_command("ping", description="Health check. Echoes the payload back with pong=true.")
def _handle_ping(payload: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True, "echo": payload}


def manifest() -> list[dict[str, Any]]:
    """List of registered command handlers, suitable for posting to the
    backend at /agents/{name}/manifest."""
    return [
        {"kind": k, "description": _descriptions.get(k)}
        for k in _handlers
    ]


class _Poller:
    def __init__(self, client, interval: float) -> None:
        self._client = client
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lightsei-commands", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception as e:
                logger.warning("lightsei command poller error: %s", e)
            self._stop.wait(self._interval)

    def _tick_once(self) -> None:
        if self._client._http is None or not self._client.agent_name:
            return
        try:
            r = self._client._http.post(
                f"/agents/{self._client.agent_name}/commands/claim",
                timeout=self._client.timeout,
            )
            if r.status_code != 200:
                return
            cmd = r.json().get("command")
        except Exception:
            return
        if cmd is None:
            return
        self._dispatch(cmd)

    def _dispatch(self, cmd: dict[str, Any]) -> None:
        kind = cmd.get("kind") or ""
        cmd_id = cmd.get("id")
        handler = _handlers.get(kind)
        if handler is None:
            self._complete(cmd_id, error=f"no handler for command kind={kind!r}")
            return
        # Set the dispatch context so any `send_command` calls made inside
        # the handler inherit the chain id. We pull from the cmd's
        # `dispatch_chain_id` if the backend returned one (Phase 11.2+);
        # otherwise we generate a fresh chain id so client-only chains still
        # group correctly in user-facing tooling that reads our SDK output.
        chain_id = cmd.get("dispatch_chain_id") or str(uuid.uuid4())
        _set_dispatch_context(
            chain_id=chain_id,
            command_id=cmd_id,
            source_agent=self._client.agent_name,
        )
        try:
            try:
                result = handler(cmd.get("payload") or {})
            except BaseException as e:
                self._complete(cmd_id, error=repr(e))
                return
            if result is not None and not isinstance(result, dict):
                result = {"value": result}
            self._complete(cmd_id, result=result)
        finally:
            _clear_dispatch_context()

    def _complete(
        self,
        cmd_id: Optional[str],
        *,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if cmd_id is None or self._client._http is None:
            return
        body: dict[str, Any] = {}
        if error is not None:
            body["error"] = error
        elif result is not None:
            body["result"] = result
        try:
            self._client._http.post(
                f"/commands/{cmd_id}/complete",
                json=body,
                timeout=self._client.timeout,
            )
        except Exception as e:
            logger.warning("lightsei failed to post command result: %s", e)


def has_handlers() -> bool:
    """True if the user (or our built-in ping) registered at least one
    handler. Used by `init()` to decide whether to start a poller.

    Always True today because of the built-in ping handler — but kept as a
    hook in case we want to make ping opt-in later.
    """
    return bool(_handlers)


# ---------- Dispatch context ---------- #


def _set_dispatch_context(
    *,
    chain_id: str,
    command_id: Optional[str],
    source_agent: Optional[str],
) -> None:
    """Stash the active dispatch chain on the calling thread.

    Called by the auto-poller's `_dispatch` and by the public
    `claim_command`. Any `send_command` calls made on the same thread while
    the context is set will inherit the chain id automatically.
    """
    _command_context.dispatch_chain_id = chain_id
    _command_context.command_id = command_id
    _command_context.source_agent = source_agent


def _clear_dispatch_context() -> None:
    """Drop the active dispatch chain. Called when a handler / claimed
    command finishes (success or failure) so the next claim doesn't
    accidentally inherit a stale chain id."""
    for attr in ("dispatch_chain_id", "command_id", "source_agent"):
        try:
            delattr(_command_context, attr)
        except AttributeError:
            pass


def current_dispatch_chain_id() -> Optional[str]:
    """Return the chain id of the command currently being handled on this
    thread, or None if no command is active. Public so user code that
    spans multiple `send_command` calls in odd ways can read + thread it
    through manually if it needs to.
    """
    return getattr(_command_context, "dispatch_chain_id", None)


def _current_source_agent() -> Optional[str]:
    """Return the agent currently handling a command on this thread."""
    return getattr(_command_context, "source_agent", None)


# ---------- Public dispatch surface ---------- #


def send_command(
    client,
    target_agent: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    dispatch_chain_id: Optional[str] = None,
    source_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Enqueue a command for `target_agent`. Returns the created command.

    Chain id resolution, in order: explicit `dispatch_chain_id` arg →
    inherited from the active claim's thread-local context → freshly
    generated UUID. Source agent attribution defaults to the active claim's
    agent, then to the agent name passed to init().

    Raises LightseiError on transport failure or non-2xx response.
    """
    from .errors import LightseiError

    if not target_agent:
        raise ValueError("send_command requires a target_agent")
    if not kind:
        raise ValueError("send_command requires a command kind")
    if client is None or client._http is None:
        raise LightseiError(
            "send_command called before lightsei.init() — "
            "no HTTP client available"
        )

    chain_id = (
        dispatch_chain_id
        or current_dispatch_chain_id()
        or str(uuid.uuid4())
    )
    resolved_source_agent = (
        source_agent
        or _current_source_agent()
        or client.agent_name
    )
    body: dict[str, Any] = {
        "kind": kind,
        "payload": payload or {},
        # Forward-compat: server in 11.1 ignores; persisted in 11.2+.
        "dispatch_chain_id": chain_id,
    }
    if resolved_source_agent:
        body["source_agent"] = resolved_source_agent
    try:
        r = client._http.post(
            f"/agents/{target_agent}/commands",
            json=body,
            timeout=client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"send_command transport error: {e}") from e
    if r.status_code >= 400:
        raise LightseiError(
            f"send_command failed: {r.status_code} {r.text[:200]}"
        )
    cmd = r.json()
    # Echo the chain id we sent back into the local copy so the caller can
    # see it even if the server hasn't started persisting the column yet.
    cmd.setdefault("dispatch_chain_id", chain_id)
    return cmd


def claim_command(
    client,
    *,
    agent_name: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending command for `agent_name`
    (defaults to the agent name passed to `init()`). Returns the command
    dict, or None when the queue is empty.

    Sets the per-thread dispatch context so any `send_command` calls made
    after claiming inherit the chain id. Call `complete_command` (or let
    a `with` block do it) to clear the context when you're done.

    Use the `@on_command` decorator if you want auto-dispatch instead;
    `claim_command` is for code that wants explicit polling control.
    """
    from .errors import LightseiError

    if client is None or client._http is None:
        raise LightseiError(
            "claim_command called before lightsei.init() — "
            "no HTTP client available"
        )
    name = agent_name or client.agent_name
    if not name:
        raise ValueError(
            "claim_command requires an agent_name "
            "(set on init() or passed explicitly)"
        )
    try:
        r = client._http.post(
            f"/agents/{name}/commands/claim",
            timeout=client.timeout,
        )
    except Exception as e:
        raise LightseiError(f"claim_command transport error: {e}") from e
    if r.status_code >= 400:
        raise LightseiError(
            f"claim_command failed: {r.status_code} {r.text[:200]}"
        )
    body = r.json() or {}
    cmd = body.get("command")
    if cmd is None:
        return None
    chain_id = cmd.get("dispatch_chain_id") or str(uuid.uuid4())
    cmd.setdefault("dispatch_chain_id", chain_id)
    _set_dispatch_context(
        chain_id=chain_id,
        command_id=cmd.get("id"),
        source_agent=name,
    )
    return cmd


def complete_command(
    client,
    command_id: str,
    *,
    result: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Mark a claimed command done (or failed). Clears this thread's
    dispatch context so the next `claim_command` starts fresh.

    Pass exactly one of `result` (success) or `error` (failure). Passing
    both is treated as failure — the explicit error wins, since that's
    what most callers actually mean when they accidentally send both.
    """
    from .errors import LightseiError

    if client is None or client._http is None:
        raise LightseiError(
            "complete_command called before lightsei.init() — "
            "no HTTP client available"
        )
    if not command_id:
        raise ValueError("complete_command requires a command_id")
    body: dict[str, Any] = {}
    if error is not None:
        body["error"] = error
    elif result is not None:
        body["result"] = result
    try:
        r = client._http.post(
            f"/commands/{command_id}/complete",
            json=body,
            timeout=client.timeout,
        )
    except Exception as e:
        _clear_dispatch_context()
        raise LightseiError(f"complete_command transport error: {e}") from e
    finally:
        _clear_dispatch_context()
    if r.status_code >= 400:
        raise LightseiError(
            f"complete_command failed: {r.status_code} {r.text[:200]}"
        )
    return r.json()
