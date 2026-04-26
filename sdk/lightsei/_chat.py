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
from typing import Any, Callable, List, Optional

logger = logging.getLogger("lightsei.chat")

# Single registered chat handler (only one per process for now).
_handler: Optional[Callable[[List[dict[str, Any]]], Any]] = None


def on_chat(fn: Callable[[List[dict[str, Any]]], Any]):
    """Register a chat handler. Replaces any previously registered one."""
    global _handler
    _handler = fn
    return fn


def has_chat_handler() -> bool:
    return _handler is not None


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
        if _handler is None:
            self._complete(message_id, error="no chat handler registered (use @lightsei.on_chat)")
            return
        try:
            result = _handler(history)
        except BaseException as e:
            self._complete(message_id, error=repr(e))
            return
        if result is None:
            self._complete(message_id, content="")
            return
        if isinstance(result, str):
            self._complete(message_id, content=result)
            return
        # Allow returning {"content": "...", ...} for forward-compat
        if isinstance(result, dict) and "content" in result:
            self._complete(message_id, content=str(result["content"]))
            return
        # Fallback: stringify whatever the handler gave us
        self._complete(message_id, content=str(result))

    def _complete(
        self,
        message_id: Optional[str],
        *,
        content: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if message_id is None or self._client._http is None:
            return
        body: dict[str, Any] = {}
        if error is not None:
            body["error"] = error
        else:
            body["content"] = content or ""
        try:
            self._client._http.post(
                f"/messages/{message_id}/complete",
                json=body,
                timeout=self._client.timeout,
            )
        except Exception as e:
            logger.warning("lightsei chat complete failed: %s", e)
