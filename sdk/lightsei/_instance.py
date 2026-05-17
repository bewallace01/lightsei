"""Per-process bot identity + heartbeat.

On init() we mint a UUID, capture hostname/pid/sdk_version, and register
the instance with the backend. A daemon thread re-posts the heartbeat on
a timer so the dashboard can show "live now" status.

Graceful degradation: every backend call is best-effort. If the network
flaps, we keep heartbeating; the next successful call refreshes status.
A failure never crashes the user's bot — *except* when the backend
explicitly refuses to register because too many concurrent instances
of the same agent are already running on this host. In that case we
raise so the runaway-process pattern fails loudly instead of silently
overlapping LLM bills.
"""
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lightsei.instance")


class TooManyInstancesError(RuntimeError):
    """Backend refused this process because the per-host concurrency
    cap is already reached for this agent. Raised from `init()` so the
    user notices instead of unknowingly running N copies in parallel."""


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


class _HeartbeatPoster:
    def __init__(
        self,
        client,
        interval_s: float,
    ) -> None:
        self._client = client
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._instance_id: str = str(uuid.uuid4())
        self._hostname: str = _hostname()
        self._pid: int = os.getpid()
        self._started_at: datetime = datetime.now(timezone.utc)

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Fire one heartbeat synchronously on start so the dashboard sees the
        # instance immediately rather than waiting for the first tick.
        # `raise_on_refusal=True`: a 409 from the backend means the host's
        # per-agent concurrency cap is hit. Bubble that up as an exception
        # from init() so the user sees it instead of silently launching
        # yet another copy.
        self._post_once(raise_on_refusal=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lightsei-instance", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Wait first so we don't double-post immediately after start().
            if self._stop.wait(self._interval):
                return
            try:
                self._post_once()
            except Exception as e:
                logger.warning("lightsei heartbeat error: %s", e)

    def _post_once(self, raise_on_refusal: bool = False) -> None:
        if self._client._http is None or not self._client.agent_name:
            return
        try:
            r = self._client._http.post(
                f"/agents/{self._client.agent_name}/instances/heartbeat",
                json={
                    "instance_id": self._instance_id,
                    "hostname": self._hostname,
                    "pid": self._pid,
                    "sdk_version": self._client.version,
                    "started_at": self._started_at.isoformat(),
                },
                timeout=self._client.timeout,
            )
        except Exception as e:
            logger.debug("lightsei heartbeat post failed: %s", e)
            return
        # 409 on the very first heartbeat means the per-host cap is hit
        # (see backend MAX_INSTANCES_PER_HOSTNAME). Surface this loudly
        # only on the synchronous startup call — for background heartbeats
        # we keep it as a debug log so a config change while a process
        # is alive doesn't kill it mid-run.
        if r.status_code == 409 and raise_on_refusal:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise TooManyInstancesError(detail or "instance registration refused")
        elif r.status_code >= 400:
            logger.debug(
                "lightsei heartbeat non-2xx: %s %s",
                r.status_code, r.text[:200],
            )
            return
        # Phase 16.3: refresh the capability cache from the heartbeat
        # response. Backend echoes the agent's current capability list
        # on every heartbeat so dashboard edits propagate within one
        # heartbeat interval (default 10s) without a separate fetch.
        try:
            body = r.json()
        except Exception:
            return
        if isinstance(body, dict):
            try:
                from ._capabilities import (
                    update_capabilities,
                    update_sensitivity_level,
                )
                update_capabilities(self._client, body.get("capabilities"))
                update_sensitivity_level(
                    self._client, body.get("sensitivity_level"),
                )
            except Exception as e:
                logger.debug(
                    "lightsei capability cache refresh failed: %s", e,
                )
