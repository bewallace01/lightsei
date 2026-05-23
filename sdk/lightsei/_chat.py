"""Chat support for agents.

Usage:
    @lightsei.on_chat
    def chat(messages):
        # messages = [{"role": "user", "content": "..."}, {"role": "assistant", ...}, ...]
        # The full thread history is delivered each turn — your bot is stateless.
        # Return the assistant's reply as a string.
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
        )
        return resp.choices[0].message.content

    lightsei.init(...)

When a user sends a message in the dashboard, the backend marks an assistant
turn `pending`. The SDK's chat poller claims it, calls your handler with the
full message history, and posts the return value back as the assistant's
content. Errors raised by the handler are recorded as the message's error.
"""
import logging
import threading
import types
from typing import Any, Callable, List, Optional

logger = logging.getLogger("lightsei.chat")

# Registered chat handlers, keyed by channel. None = the default
# channel (the dashboard chat thread surface from earlier phases);
# `"widget"` = the Phase 21 customer-facing widget surface.
# Each process can have at most one handler per channel; re-decorating
# replaces.
_handlers: dict[Optional[str], Callable[..., Any]] = {}


def on_chat(arg=None):
    """Register a chat handler.

    Two call shapes:

    - `@lightsei.on_chat` (no parens) — registers as the default
      handler. Backward-compatible with pre-21.5 callers.

          @lightsei.on_chat
          def handle(messages):
              return reply(messages)

    - `@lightsei.on_chat("widget")` — registers for a specific
      channel. The Phase 21.6 widget orchestrator looks up the
      "widget" handler when dispatching a widget conversation.
      Future protocols can add their own channel names without
      colliding with the default handler.

          @lightsei.on_chat("widget")
          def handle(turn):
              # turn = {conversation_id, user_message, conversation_history}
              return reply(turn)

    Re-decorating replaces any previously-registered handler for
    that channel.
    """
    if callable(arg):
        # `@on_chat` (no parens) — `arg` is the function itself.
        _handlers[None] = arg
        return arg
    # `@on_chat("widget")` — `arg` is the channel name (or None);
    # return a decorator that registers when applied.
    channel = arg

    def decorator(fn: Callable[..., Any]):
        _handlers[channel] = fn
        return fn

    return decorator


def has_chat_handler(channel: Optional[str] = None) -> bool:
    return channel in _handlers


def get_chat_handler(channel: Optional[str] = None) -> Optional[Callable[..., Any]]:
    """Lookup the handler registered for `channel`. Returns None if
    no handler is registered for that channel. The Phase 21.6
    widget orchestrator uses this to dispatch the bot side of a
    widget conversation."""
    return _handlers.get(channel)


# Back-compat: existing code (and the chat poller below) reads
# `_handler` directly. Keep the global pointing at the default
# (None-channel) handler.
def _default_handler() -> Optional[Callable[..., Any]]:
    return _handlers.get(None)


# Module-level shim so `_chat._handler` keeps working for the older
# dashboard-chat poll path. New code should call `get_chat_handler`.
class _DefaultHandlerProxy:
    def __bool__(self) -> bool:
        return _default_handler() is not None

    def __call__(self, *args, **kwargs):
        fn = _default_handler()
        if fn is None:
            raise RuntimeError("no default chat handler registered")
        return fn(*args, **kwargs)


_handler = _DefaultHandlerProxy()


# ---------- Phase 21.6: widget.chat bridge command handler ---------- #

# Built-in @on_command("widget.chat") that bridges from the
# backend orchestrator's Command-dispatch to the user-registered
# @on_chat("widget") handler. Always registered (importing this
# module registers it as a side effect, same pattern as the
# @on_command("ping") helper in _commands.py); the bridge is a
# clean no-op if no widget handler is registered, so registering
# it unconditionally doesn't harm bots that don't use the widget.

def _widget_chat_bridge(payload):
    """Internal: handle a `widget.chat` command from the 21.6
    orchestrator. Calls the user-registered @on_chat("widget")
    handler with a structured `turn` dict, posts the reply via
    lightsei.respond, and translates exceptions into escalations.

    Always returns a dict (or None) — command-handler contract.
    """
    from . import respond as _respond_helper, escalate as _escalate_helper
    from .errors import LightseiEscalate, LightseiError

    conversation_id = (payload or {}).get("conversation_id")
    if not conversation_id:
        return {"ok": False, "error": "missing conversation_id in payload"}

    handler = get_chat_handler("widget")
    if handler is None:
        # No widget handler registered on this bot. The orchestrator
        # already checked the capability allow-list, so this is a
        # bot-side wiring mistake. Escalate so an operator sees it
        # in /inbox instead of the conversation going silent.
        try:
            _escalate_helper(
                conversation_id,
                reason="bot_unconfigured",
                payload={
                    "hint": "bot has the widget:respond capability but "
                            "no @lightsei.on_chat('widget') handler is "
                            "registered in its code."
                },
            )
        except Exception as exc:
            logger.warning(
                "widget.chat bridge: escalate call failed: %s", exc,
            )
        return {"ok": False, "error": "no_widget_handler_registered"}

    turn = {
        "conversation_id": conversation_id,
        "user_message": (payload or {}).get("user_message", ""),
        "conversation_history": (payload or {}).get("conversation_history") or [],
    }

    try:
        result = handler(turn)
    except LightseiEscalate as exc:
        # Bot raised LightseiEscalate from inside the @on_chat
        # handler. Route to the escalate endpoint with the exception's
        # reason + payload.
        try:
            _escalate_helper(
                conversation_id,
                reason=exc.reason,
                payload=exc.payload,
            )
        except LightseiError as inner:
            logger.warning(
                "widget.chat bridge: escalate after LightseiEscalate "
                "failed: %s", inner,
            )
            return {"ok": False, "escalated": False, "error": str(inner)}
        return {"ok": True, "escalated": True, "reason": exc.reason}
    except BaseException as exc:
        # Uncaught exception. Surface as bot_crash escalation so the
        # operator gets pulled in rather than the user staring at a
        # silent conversation. Don't let an exception in escalate()
        # propagate either — graceful degradation per CLAUDE.md.
        logger.exception(
            "widget.chat bridge: handler raised; escalating as bot_crash"
        )
        try:
            _escalate_helper(
                conversation_id,
                reason="bot_crash",
                payload={"error": repr(exc)},
            )
        except Exception:
            pass
        return {"ok": False, "escalated": True, "error": repr(exc)}

    # Successful return. Two shapes accepted: a string reply or
    # None (handler decided not to reply this turn). Anything else
    # is a bot-author bug, surface it cleanly.
    if result is None:
        return {"ok": True, "no_reply": True}
    if not isinstance(result, str):
        logger.warning(
            "widget.chat bridge: handler returned %s; expected str or None",
            type(result).__name__,
        )
        return {
            "ok": False,
            "error": (
                f"@on_chat('widget') handler must return a string or None; "
                f"got {type(result).__name__}"
            ),
        }
    if not result.strip():
        return {"ok": True, "no_reply": True}

    try:
        _respond_helper(conversation_id, result)
    except LightseiError as exc:
        logger.warning(
            "widget.chat bridge: respond call failed: %s", exc,
        )
        return {"ok": False, "responded": False, "error": str(exc)}
    return {"ok": True, "responded": True}


def _register_widget_chat_bridge() -> None:
    """Side-effect: register the bridge with the SDK's command
    handler registry. Idempotent — re-importing _chat.py is safe.
    Kept in a function so test code can re-register after clearing
    the registry."""
    from ._commands import on_command
    on_command(
        "widget.chat",
        description=(
            "Internal: widget conversation turn from the Phase 21.6 "
            "orchestrator. Calls @lightsei.on_chat('widget')."
        ),
    )(_widget_chat_bridge)


_register_widget_chat_bridge()


class _ChatPoller:
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
            target=self._loop, name="lightsei-chat", daemon=True,
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
                logger.warning("lightsei chat poller error: %s", e)
            self._stop.wait(self._interval)

    def _tick_once(self) -> None:
        if self._client._http is None or not self._client.agent_name:
            return
        try:
            r = self._client._http.post(
                f"/agents/{self._client.agent_name}/threads/claim",
                timeout=self._client.timeout,
            )
            if r.status_code != 200:
                return
            turn = r.json().get("turn")
        except Exception:
            return
        if turn is None:
            return
        self._dispatch(turn)

    def _dispatch(self, turn: dict[str, Any]) -> None:
        message_id = turn.get("message_id")
        history: List[dict[str, Any]] = turn.get("messages") or []
        handler = _default_handler()
        if handler is None:
            self._complete(message_id, error="no chat handler registered (use @lightsei.on_chat)")
            return
        try:
            result = handler(history)
        except BaseException as e:
            self._complete(message_id, error=repr(e))
            return
        # Streaming: handler returned a generator/iterator. Post each yield
        # as a delta chunk; after the iterator is exhausted, mark the message
        # complete (server already has the accumulated content).
        if isinstance(result, types.GeneratorType) or (
            hasattr(result, "__iter__") and not isinstance(result, (str, bytes, dict, list))
        ):
            try:
                for chunk in result:
                    if not chunk:
                        continue
                    self._post_chunk(message_id, str(chunk))
            except BaseException as e:
                self._complete(message_id, error=repr(e))
                return
            self._complete(message_id)  # keep server-accumulated content
            return
        if result is None:
            self._complete(message_id, content="")
            return
        if isinstance(result, str):
            self._complete(message_id, content=result)
            return
        if isinstance(result, dict) and "content" in result:
            self._complete(message_id, content=str(result["content"]))
            return
        self._complete(message_id, content=str(result))

    def _post_chunk(self, message_id: Optional[str], delta: str) -> None:
        if message_id is None or self._client._http is None:
            return
        try:
            self._client._http.post(
                f"/messages/{message_id}/chunk",
                json={"delta": delta},
                timeout=self._client.timeout,
            )
        except Exception as e:
            logger.warning("lightsei chat chunk post failed: %s", e)

    _SENTINEL = object()

    def _complete(
        self,
        message_id: Optional[str],
        *,
        content: Any = _SENTINEL,
        error: Optional[str] = None,
    ) -> None:
        """If content is not passed (sentinel), the server keeps whatever
        it accumulated from chunks. Pass content=None or '' to explicitly
        clear; pass a string to overwrite."""
        if message_id is None or self._client._http is None:
            return
        body: dict[str, Any] = {}
        if error is not None:
            body["error"] = error
        elif content is not self._SENTINEL:
            body["content"] = content if content is not None else ""
        try:
            self._client._http.post(
                f"/messages/{message_id}/complete",
                json=body,
                timeout=self._client.timeout,
            )
        except Exception as e:
            logger.warning("lightsei chat complete failed: %s", e)
