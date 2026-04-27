"""Lightsei Phase 5.3 worker.

Polls the backend for queued deployments, builds a venv per deployment,
spawns the bot, streams logs back. Single host, no isolation. See
`MEMORY.md` "Runtime decision (2026-04-27)" for the architectural call to
build this in-process for v1 and swap for managed isolation in 5B.

Required env:
  LIGHTSEI_WORKER_TOKEN     shared secret matching the backend's value
  LIGHTSEI_BASE_URL         backend URL, e.g. https://api.lightsei.com

Optional env:
  LIGHTSEI_WORKER_SCRATCH   default /tmp/lightsei-worker
  LIGHTSEI_WORKER_POLL_S    default 5.0
  LIGHTSEI_WORKER_MAX_CONCURRENT  default 4
  LIGHTSEI_WORKER_PIP_TIMEOUT_S   default 300

Usage:
  python worker/runner.py

The worker authenticates as a system component, NOT a workspace user. A
stolen worker token grants cross-tenant access — keep it in the same
class of secrets as LIGHTSEI_SECRETS_KEY.

Bots inherit:
  - Every workspace secret as an env var (so e.g. OPENAI_API_KEY just works)
  - LIGHTSEI_AGENT_NAME pinned to the deployment's agent
  - LIGHTSEI_BASE_URL inherited from the worker
The user is expected to also store a workspace api key as the secret
LIGHTSEI_API_KEY so the bot can authenticate to the backend (heartbeat,
events, get_secret, etc.) — auto-minting will land in a follow-up.
"""
import io
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("lightsei.worker")


# ---------- config ----------

POLL_INTERVAL_S = float(os.environ.get("LIGHTSEI_WORKER_POLL_S", "5"))
HEARTBEAT_INTERVAL_S = 30.0
LOG_FLUSH_INTERVAL_S = 1.0
LOG_BATCH_SIZE = 100
PIP_TIMEOUT_S = float(os.environ.get("LIGHTSEI_WORKER_PIP_TIMEOUT_S", "300"))
MAX_CONCURRENT = int(os.environ.get("LIGHTSEI_WORKER_MAX_CONCURRENT", "4"))
SCRATCH_BASE = Path(
    os.environ.get("LIGHTSEI_WORKER_SCRATCH", "/tmp/lightsei-worker")
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- backend client ----------

class WorkerClient:
    """Thin httpx wrapper around /worker/* endpoints. All methods are
    intentionally synchronous — the supervisor uses threads, not asyncio.

    For tests, pass `http=` an httpx.Client (or compatible TestClient) that
    already has the auth header baked in."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_s: float = 30.0,
        http: Optional[httpx.Client] = None,
    ) -> None:
        if http is not None:
            self.http = http
            self._owned = False
        else:
            assert base_url and token, "either http or (base_url, token) required"
            self.http = httpx.Client(
                base_url=base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout_s,
            )
            self._owned = True

    def claim(self, worker_id: str) -> Optional[dict[str, Any]]:
        r = self.http.post(
            "/worker/deployments/claim", params={"worker_id": worker_id}
        )
        r.raise_for_status()
        body = r.json()
        return body if body.get("deployment") else None

    def status(
        self, deployment_id: str, status: str, error: Optional[str] = None
    ) -> None:
        r = self.http.post(
            f"/worker/deployments/{deployment_id}/status",
            json={"status": status, "error": error},
        )
        r.raise_for_status()

    def heartbeat(self, deployment_id: str) -> dict[str, Any]:
        r = self.http.post(
            f"/worker/deployments/{deployment_id}/heartbeat",
        )
        r.raise_for_status()
        return r.json()

    def append_logs(
        self, deployment_id: str, lines: list[dict[str, Any]]
    ) -> None:
        if not lines:
            return
        r = self.http.post(
            f"/worker/deployments/{deployment_id}/logs",
            json={"lines": lines},
        )
        r.raise_for_status()

    def get_blob(self, blob_id: str) -> bytes:
        r = self.http.get(f"/worker/blobs/{blob_id}")
        r.raise_for_status()
        return r.content

    def get_workspace_secrets(self, workspace_id: str) -> dict[str, str]:
        r = self.http.get(f"/worker/workspaces/{workspace_id}/secrets")
        r.raise_for_status()
        return r.json()["secrets"]

    def close(self) -> None:
        if not self._owned:
            return
        try:
            self.http.close()
        except Exception:
            pass


# ---------- supervisor ----------

class DeploymentSupervisor:
    """One per active deployment. Owns the bot subprocess, log threads,
    heartbeat thread, and lifecycle. Runs to completion in its own thread.
    """

    def __init__(
        self,
        client: WorkerClient,
        deployment: dict[str, Any],
        workspace_id: str,
    ) -> None:
        self.client = client
        self.deployment = deployment
        self.workspace_id = workspace_id
        self.id = deployment["id"]
        self.agent_name = deployment["agent_name"]
        self.scratch = SCRATCH_BASE / self.id
        self.proc: Optional[subprocess.Popen] = None
        self.log_q: queue.Queue = queue.Queue(maxsize=10_000)
        self.stop_event = threading.Event()
        self.user_wants_stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def run(self) -> None:
        """Top-level entry. Build, spawn, supervise, clean up — never
        raises (all errors get reported to the backend as a failed status)."""
        try:
            python, bot_dir = self._build()
            self._spawn(python, bot_dir)
            self._supervise()
        except _SetupError as e:
            self._log_system(f"setup failed: {e}")
            self._safe_status("failed", error=str(e))
        except Exception as e:
            logger.exception("unexpected error in deployment %s", self.id)
            self._log_system(f"unexpected error: {e!r}")
            self._safe_status("failed", error=f"unexpected error: {e!r}")
        finally:
            self._cleanup()

    # --- setup ---

    def _build(self) -> tuple[Path, Path]:
        self._log_system(f"deployment {self.id} starting on worker")
        self._safe_status("building")

        blob_id = self.deployment.get("source_blob_id")
        if not blob_id:
            raise _SetupError("deployment has no source blob")

        self.scratch.mkdir(parents=True, exist_ok=True)
        bot_dir = self.scratch / "src"
        if bot_dir.exists():
            shutil.rmtree(bot_dir)
        bot_dir.mkdir()

        self._log_system(f"fetching bundle {blob_id[:8]}…")
        try:
            data = self.client.get_blob(blob_id)
        except Exception as e:
            raise _SetupError(f"failed to fetch bundle: {e}") from e
        self._log_system(f"unpacking {len(data)} bytes")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(bot_dir)
        except zipfile.BadZipFile as e:
            raise _SetupError(f"bundle is not a valid zip: {e}") from e

        venv_dir = self.scratch / ".venv"
        if not (venv_dir / "bin" / "python").exists():
            self._log_system("creating venv")
            venv.create(str(venv_dir), with_pip=True)
        python = venv_dir / "bin" / "python"

        req = bot_dir / "requirements.txt"
        if req.exists():
            self._log_system("pip install -r requirements.txt")
            cp = subprocess.run(
                [str(python), "-m", "pip", "install", "-q", "-r", str(req)],
                cwd=str(bot_dir),
                capture_output=True, text=True, timeout=PIP_TIMEOUT_S,
            )
            if cp.returncode != 0:
                # Capture trailing chunk of stderr so the user sees what
                # broke. Truncate to keep the log row size sane.
                tail = (cp.stderr or "").strip()[-1500:]
                self._log_system(f"pip install failed:\n{tail}")
                raise _SetupError(f"pip install exit {cp.returncode}")

        return python, bot_dir

    # --- spawn + supervise ---

    def _spawn(self, python: Path, bot_dir: Path) -> None:
        entry = bot_dir / "bot.py"
        if not entry.exists():
            raise _SetupError("no bot.py at the root of the bundle")

        self._log_system("fetching workspace secrets")
        try:
            secrets = self.client.get_workspace_secrets(self.workspace_id)
        except Exception as e:
            raise _SetupError(f"failed to fetch secrets: {e}") from e

        env = {**os.environ, **secrets}
        env["LIGHTSEI_AGENT_NAME"] = self.agent_name
        env.setdefault(
            "LIGHTSEI_BASE_URL",
            os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com"),
        )

        if "LIGHTSEI_API_KEY" not in env:
            self._log_system(
                "warning: LIGHTSEI_API_KEY is not in workspace secrets; "
                "the bot's SDK calls will fail. Add it via the dashboard "
                "secrets panel."
            )

        self._log_system(f"starting {entry.name}")
        try:
            self.proc = subprocess.Popen(
                [str(python), "-u", str(entry)],
                cwd=str(bot_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            raise _SetupError(f"failed to spawn bot: {e}") from e
        self._log_system(f"pid={self.proc.pid}")

        # Background threads: log tail (stdout, stderr), log flusher, heartbeat.
        self.threads.append(threading.Thread(
            target=self._tail, args=(self.proc.stdout, "stdout"),
            name=f"tail-out-{self.id[:8]}", daemon=True,
        ))
        self.threads.append(threading.Thread(
            target=self._tail, args=(self.proc.stderr, "stderr"),
            name=f"tail-err-{self.id[:8]}", daemon=True,
        ))
        self.threads.append(threading.Thread(
            target=self._log_flusher,
            name=f"flush-{self.id[:8]}", daemon=True,
        ))
        self.threads.append(threading.Thread(
            target=self._heartbeater,
            name=f"hb-{self.id[:8]}", daemon=True,
        ))
        for t in self.threads:
            t.start()

        self._safe_status("running")

    def _supervise(self) -> None:
        assert self.proc is not None
        rc: Optional[int] = None
        while rc is None:
            try:
                rc = self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if self.user_wants_stop.is_set():
                    self._log_system("desired_state=stopped; terminating bot")
                    self._terminate_proc()
                    rc = self.proc.wait(timeout=10)
                    if rc is None:
                        # belt and suspenders; Popen.wait above already
                        # handles it but be explicit.
                        rc = -1
                    break

        if self.user_wants_stop.is_set():
            self._log_system("bot stopped on user request")
            self._safe_status("stopped")
        elif rc == 0:
            self._log_system("bot exited cleanly")
            self._safe_status("stopped")
        else:
            self._log_system(f"bot exited rc={rc}")
            self._safe_status("failed", error=f"bot exited rc={rc}")

    # --- log streaming ---

    def _tail(self, stream, kind: str) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            self._enqueue_log(kind, line)

    def _enqueue_log(self, stream: str, line: str) -> None:
        try:
            self.log_q.put_nowait(
                {"stream": stream, "line": line, "ts": _utcnow_iso()}
            )
        except queue.Full:
            # Drop on overflow; we'll resume cleanly once the flusher catches up.
            pass

    def _log_system(self, line: str) -> None:
        logger.info("[%s] %s", self.id[:8], line)
        self._enqueue_log("system", line)

    def _log_flusher(self) -> None:
        while not self.stop_event.is_set():
            self._flush_logs(LOG_BATCH_SIZE)
            self.stop_event.wait(LOG_FLUSH_INTERVAL_S)
        # final drain
        self._flush_logs(10_000)

    def _flush_logs(self, max_items: int) -> None:
        batch: list[dict[str, Any]] = []
        while len(batch) < max_items:
            try:
                batch.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        try:
            self.client.append_logs(self.id, batch)
        except Exception as e:
            logger.warning("[%s] log append failed: %s", self.id[:8], e)

    # --- heartbeat ---

    def _heartbeater(self) -> None:
        while not self.stop_event.is_set():
            try:
                row = self.client.heartbeat(self.id)
                if row.get("desired_state") == "stopped":
                    self.user_wants_stop.set()
            except Exception as e:
                logger.warning("[%s] heartbeat failed: %s", self.id[:8], e)
            self.stop_event.wait(HEARTBEAT_INTERVAL_S)

    # --- helpers ---

    def _terminate_proc(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass

    def _safe_status(self, status: str, error: Optional[str] = None) -> None:
        try:
            self.client.status(self.id, status, error=error)
        except Exception as e:
            logger.warning("[%s] status update failed: %s", self.id[:8], e)

    def _cleanup(self) -> None:
        self.stop_event.set()
        self._terminate_proc()
        for t in self.threads:
            t.join(timeout=3.0)
        # leave self.scratch on disk for postmortem; periodic GC is a future
        # phase concern.


class _SetupError(Exception):
    """Recoverable setup failure — surfaced as a failed deployment, not a
    worker crash."""


# ---------- main loop ----------

def _run_loop(
    client: WorkerClient,
    worker_id: str,
    stop: threading.Event,
    poll_interval_s: float = POLL_INTERVAL_S,
    max_concurrent: int = MAX_CONCURRENT,
) -> None:
    active: dict[str, threading.Thread] = {}

    while not stop.is_set():
        # Reap finished supervisors.
        for dep_id in list(active):
            t = active[dep_id]
            if not t.is_alive():
                t.join()
                del active[dep_id]

        if len(active) < max_concurrent:
            try:
                claimed = client.claim(worker_id)
            except Exception as e:
                logger.warning("claim failed: %s", e)
                claimed = None
            if claimed:
                dep = claimed["deployment"]
                ws_id = claimed["workspace_id"]
                logger.info(
                    "claimed deployment=%s agent=%s workspace=%s",
                    dep["id"], dep["agent_name"], ws_id,
                )
                supervisor = DeploymentSupervisor(client, dep, ws_id)
                t = threading.Thread(
                    target=supervisor.run,
                    name=f"sup-{dep['id'][:8]}",
                    daemon=False,
                )
                t.start()
                active[dep["id"]] = t
                # Don't sleep — try to grab the next pending immediately.
                continue
        stop.wait(poll_interval_s)

    logger.info("worker shutting down; %d active deployment(s)", len(active))
    for dep_id, t in active.items():
        logger.info("waiting on supervisor %s", dep_id[:8])
        t.join(timeout=15.0)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    base_url = os.environ.get("LIGHTSEI_BASE_URL", "http://localhost:8000")
    token = os.environ.get("LIGHTSEI_WORKER_TOKEN")
    if not token:
        print("LIGHTSEI_WORKER_TOKEN must be set", file=sys.stderr)
        return 2

    SCRATCH_BASE.mkdir(parents=True, exist_ok=True)

    worker_id = str(uuid.uuid4())
    client = WorkerClient(base_url, token)
    stop = threading.Event()

    def on_signal(sig, _frame):
        logger.info("worker stopping (signal=%s)", sig)
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    logger.info(
        "lightsei worker %s starting; base_url=%s scratch=%s",
        worker_id, base_url, SCRATCH_BASE,
    )

    try:
        _run_loop(client, worker_id, stop)
    finally:
        client.close()

    logger.info("worker %s stopped", worker_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
