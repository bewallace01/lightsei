import atexit
import logging
import os
import queue
import threading
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("lightsei")

_DEFAULT_BASE_URL = os.environ.get("LIGHTSEI_BASE_URL", "http://localhost:8000")
_DEFAULT_FLUSH_INTERVAL = 1.0
_DEFAULT_BATCH_SIZE = 100
_DEFAULT_TIMEOUT = 5.0
_DEFAULT_MAX_RETRIES = 3
_MAX_QUEUE_SIZE = 10_000
_DEFAULT_CAPTURE_CONTENT = True
_DEFAULT_COMMAND_POLL_INTERVAL = 5.0
_DEFAULT_CHAT_POLL_INTERVAL = 2.0


class _Client:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self.api_key: Optional[str] = None
        self.agent_name: Optional[str] = None
        self.version: str = "0.0.0"
        self.base_url: str = _DEFAULT_BASE_URL
        self.flush_interval: float = _DEFAULT_FLUSH_INTERVAL
        self.batch_size: int = _DEFAULT_BATCH_SIZE
        self.timeout: float = _DEFAULT_TIMEOUT
        self.max_retries: int = _DEFAULT_MAX_RETRIES
        self.capture_content: bool = _DEFAULT_CAPTURE_CONTENT
        self.command_poll_interval: float = _DEFAULT_COMMAND_POLL_INTERVAL
        self.chat_poll_interval: float = _DEFAULT_CHAT_POLL_INTERVAL
        self._queue: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._http: Optional[httpx.Client] = None
        self._stop_event = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._atexit_registered = False
        self._command_poller = None  # set in init() if needed
        self._chat_poller = None     # set in init() if needed

    def is_initialized(self) -> bool:
        return self._initialized

    def init(
        self,
        api_key: Optional[str],
        agent_name: Optional[str],
        version: str = "0.0.0",
        base_url: Optional[str] = None,
        flush_interval: Optional[float] = None,
        batch_size: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        capture_content: Optional[bool] = None,
        command_poll_interval: Optional[float] = None,
        chat_poll_interval: Optional[float] = None,
    ) -> None:
        with self._lock:
            if self._initialized:
                logger.debug("lightsei.init() called again; ignoring")
                return
            self.api_key = api_key
            self.agent_name = agent_name
            self.version = version
            if base_url:
                self.base_url = base_url
            if flush_interval is not None:
                self.flush_interval = flush_interval
            if batch_size is not None:
                self.batch_size = batch_size
            if timeout is not None:
                self.timeout = timeout
            if max_retries is not None:
                self.max_retries = max_retries
            if capture_content is not None:
                self.capture_content = capture_content
            if command_poll_interval is not None:
                self.command_poll_interval = command_poll_interval
            if chat_poll_interval is not None:
                self.chat_poll_interval = chat_poll_interval

            headers = {"content-type": "application/json"}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            self._http = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=headers,
            )

            self._stop_event = threading.Event()
            self._flush_thread = threading.Thread(
                target=self._flush_loop,
                name="lightsei-flush",
                daemon=True,
            )
            self._flush_thread.start()

            if not self._atexit_registered:
                atexit.register(self.shutdown)
                self._atexit_registered = True

            # Optional: start the command poller if any handlers are registered
            # AND we have an agent_name to scope by.
            try:
                from ._commands import _Poller, has_handlers, manifest
                if has_handlers() and self.agent_name:
                    # Publish the handler manifest so the dashboard can show a
                    # dropdown of valid kinds. Best-effort.
                    try:
                        self._http.put(
                            f"/agents/{self.agent_name}/manifest",
                            json={"command_handlers": manifest()},
                            timeout=self.timeout,
                        )
                    except Exception as e:
                        logger.warning("lightsei manifest publish failed: %s", e)
                    self._command_poller = _Poller(self, self.command_poll_interval)
                    self._command_poller.start()
            except Exception as e:  # pragma: no cover
                logger.warning("lightsei command poller failed to start: %s", e)

            # Optional: start the chat poller if @on_chat is registered.
            try:
                from ._chat import _ChatPoller, has_chat_handler
                if has_chat_handler() and self.agent_name:
                    self._chat_poller = _ChatPoller(self, self.chat_poll_interval)
                    self._chat_poller.start()
            except Exception as e:  # pragma: no cover
                logger.warning("lightsei chat poller failed to start: %s", e)

            self._initialized = True
            logger.info(
                "lightsei initialized agent=%s version=%s base_url=%s",
                agent_name, version, self.base_url,
            )

    def emit(
        self,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        run_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        if not self._initialized:
            logger.debug("lightsei.emit before init; dropping kind=%s", kind)
            return
        from ._context import get_run_id

        rid = run_id or get_run_id()
        if rid is None:
            logger.debug("lightsei.emit with no run_id; dropping kind=%s", kind)
            return
        event: dict[str, Any] = {
            "run_id": rid,
            "agent_name": agent_name or self.agent_name,
            "kind": kind,
            "payload": payload or {},
        }
        if timestamp:
            event["timestamp"] = timestamp
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("lightsei event queue full; dropping kind=%s", kind)

        # opportunistic flush trigger when over batch size
        if self._queue.qsize() >= self.batch_size:
            self._wake_flush()

    def check_policy(
        self,
        action: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        run_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self._initialized or self._http is None:
            return {"allow": True}
        body = {
            "agent_name": agent_name or self.agent_name,
            "run_id": run_id,
            "action": action,
            "payload": payload or {},
        }
        try:
            r = self._http.post("/policy/check", json=body, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # graceful degradation: fail open
            logger.warning("lightsei policy check failed (fail-open): %s", e)
            return {"allow": True}

    def flush(self, timeout: float = 2.0) -> None:
        if not self._initialized:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = self._drain(self.batch_size)
            if not batch:
                return
            self._send_batch(batch)

    def _drain(self, max_items: int) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while len(batch) < max_items:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _wake_flush(self) -> None:
        # The flush thread wakes on `_stop_event.wait(interval)`. Setting the
        # event would stop the thread, so we just rely on the next tick.
        # Kept as a hook in case we want a faster wakeup later.
        return

    def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            triggered = self._stop_event.wait(self.flush_interval)
            try:
                batch = self._drain(self.batch_size)
                if batch:
                    self._send_batch(batch)
            except Exception as e:  # belt and suspenders
                logger.warning("lightsei flush loop error: %s", e)
            if triggered:
                # one final drain after stop signal so shutdown flushes
                try:
                    batch = self._drain(self.batch_size)
                    if batch:
                        self._send_batch(batch)
                except Exception:
                    pass
                return

    def _send_batch(self, batch: list[dict[str, Any]]) -> None:
        for event in batch:
            self._post_event(event)

    def _post_event(self, event: dict[str, Any]) -> None:
        if self._http is None:
            return
        for attempt in range(self.max_retries):
            try:
                r = self._http.post("/events", json=event)
                r.raise_for_status()
                return
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.warning(
                        "lightsei failed to post event after %d attempts: %s",
                        self.max_retries, e,
                    )
                    return
                time.sleep(min(0.1 * (2 ** attempt), 1.0))

    def shutdown(self) -> None:
        if not self._initialized:
            return
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=2.0)
        if self._command_poller is not None:
            try:
                self._command_poller.stop()
            except Exception:
                pass
        if self._chat_poller is not None:
            try:
                self._chat_poller.stop()
            except Exception:
                pass
        try:
            self.flush(timeout=2.0)
        except Exception:
            pass
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._flush_thread and self._flush_thread.is_alive():
                self._flush_thread.join(timeout=2.0)
            if self._command_poller is not None:
                try:
                    self._command_poller.stop()
                except Exception:
                    pass
            if self._chat_poller is not None:
                try:
                    self._chat_poller.stop()
                except Exception:
                    pass
            if self._http is not None:
                try:
                    self._http.close()
                except Exception:
                    pass
            self._initialized = False
            self.api_key = None
            self.agent_name = None
            self.version = "0.0.0"
            self.base_url = _DEFAULT_BASE_URL
            self.flush_interval = _DEFAULT_FLUSH_INTERVAL
            self.batch_size = _DEFAULT_BATCH_SIZE
            self.timeout = _DEFAULT_TIMEOUT
            self.max_retries = _DEFAULT_MAX_RETRIES
            self.capture_content = _DEFAULT_CAPTURE_CONTENT
            self.command_poll_interval = _DEFAULT_COMMAND_POLL_INTERVAL
            self.chat_poll_interval = _DEFAULT_CHAT_POLL_INTERVAL
            self._queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
            self._http = None
            self._stop_event = threading.Event()
            self._flush_thread = None
            self._command_poller = None
            self._chat_poller = None


_client = _Client()
