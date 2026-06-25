"""Phase 5.3 worker integration tests.

The worker's WorkerClient is constructed against the in-process TestClient
instead of a real HTTP server, so each test exercises the full
claim → setup → spawn → log → status flow without mocks.
"""
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

import runner  # from worker/ (added to pythonpath in backend/pytest.ini)
from tests.conftest import auth_headers


def _make_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


@pytest.fixture()
def worker_client(client):
    """A WorkerClient backed by the FastAPI TestClient with the worker
    bearer pre-attached."""
    client.headers.update({"Authorization": "Bearer test-worker-token"})
    return runner.WorkerClient(http=client)


def _upload_bot(client, headers, agent_name, files):
    bundle = _make_zip_bytes(files)
    r = client.post(
        "/workspaces/me/deployments",
        headers=headers,
        data={"agent_name": agent_name},
        files={"bundle": ("b.zip", io.BytesIO(bundle), "application/zip")},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _tight_intervals(monkeypatch, tmp_path):
    """Speed up the heartbeat + log flusher so tests don't hang."""
    monkeypatch.setattr(runner, "SCRATCH_BASE", tmp_path / "worker-scratch")
    monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL_S", 0.5)
    monkeypatch.setattr(runner, "LOG_FLUSH_INTERVAL_S", 0.1)


def test_supervisor_runs_clean_bot_to_completion(client, alice, worker_client):
    h = auth_headers(alice["session_token"])
    bot_py = (
        "import sys\n"
        "print('hello from the bot', flush=True)\n"
        "print('warn line', file=sys.stderr, flush=True)\n"
    )
    dep = _upload_bot(
        client, h, "test-bot",
        {"bot.py": bot_py, "requirements.txt": ""},
    )

    claimed = worker_client.claim("worker-test")
    assert claimed is not None
    assert claimed["deployment"]["id"] == dep["id"]

    sup = runner.DeploymentSupervisor(
        worker_client, claimed["deployment"], claimed["workspace_id"],
    )
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "supervisor did not finish in time"

    final = client.get(
        f"/workspaces/me/deployments/{dep['id']}", headers=h,
    ).json()
    assert final["status"] == "stopped", final
    assert final["started_at"] is not None
    assert final["stopped_at"] is not None

    # Logs landed in the DB.
    from sqlalchemy import text
    from db import engine
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT stream, line FROM deployment_logs "
                "WHERE deployment_id = :id ORDER BY id"
            ),
            {"id": dep["id"]},
        ).all()
    assert any(
        r[0] == "stdout" and r[1] == "hello from the bot" for r in rows
    ), rows
    assert any(
        r[0] == "stderr" and r[1] == "warn line" for r in rows
    ), rows
    # System lines (build progress) are also recorded.
    assert any(r[0] == "system" for r in rows)


def test_supervisor_records_failed_when_bot_crashes(client, alice, worker_client):
    h = auth_headers(alice["session_token"])
    dep = _upload_bot(
        client, h, "crash-bot",
        {"bot.py": "import sys; sys.exit(7)\n", "requirements.txt": ""},
    )

    claimed = worker_client.claim("worker-test")
    sup = runner.DeploymentSupervisor(
        worker_client, claimed["deployment"], claimed["workspace_id"],
    )
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive()

    final = client.get(
        f"/workspaces/me/deployments/{dep['id']}", headers=h,
    ).json()
    assert final["status"] == "failed"
    assert "rc=7" in (final["error"] or "")


def test_supervisor_fails_on_missing_bot_py(client, alice, worker_client):
    h = auth_headers(alice["session_token"])
    dep = _upload_bot(
        client, h, "no-entry",
        {"README.md": "no bot here"},
    )

    claimed = worker_client.claim("worker-test")
    sup = runner.DeploymentSupervisor(
        worker_client, claimed["deployment"], claimed["workspace_id"],
    )
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()

    final = client.get(
        f"/workspaces/me/deployments/{dep['id']}", headers=h,
    ).json()
    assert final["status"] == "failed"
    assert "no bot.py" in (final["error"] or "")


def test_supervisor_heartbeats_during_slow_build(monkeypatch, tmp_path):
    bundle = _make_zip_bytes(
        {"bot.py": "print('ok')\n", "requirements.txt": "slow-package\n"}
    )

    class FakeBuildClient:
        def __init__(self):
            self.heartbeats: list[float] = []
            self.statuses: list[str] = []
            self.logs: list[dict] = []

        def status(self, deployment_id, status, error=None):
            self.statuses.append(status)

        def heartbeat(self, deployment_id):
            self.heartbeats.append(time.monotonic())
            return {"desired_state": "running"}

        def append_logs(self, deployment_id, lines):
            self.logs.extend(lines)

        def get_blob(self, blob_id):
            return bundle

        def get_workspace_secrets(self, workspace_id):
            return {}

    def fake_venv_create(path, with_pip=True):
        bin_dir = Path(path) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").write_text("")

    def slow_pip_install(*args, **kwargs):
        time.sleep(0.08)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(runner, "SCRATCH_BASE", tmp_path / "worker-scratch")
    monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL_S", 0.02)
    monkeypatch.setattr(runner, "LOG_FLUSH_INTERVAL_S", 0.02)
    monkeypatch.setattr(runner.venv, "create", fake_venv_create)
    monkeypatch.setattr(runner.subprocess, "run", slow_pip_install)
    monkeypatch.setattr(
        runner.DeploymentSupervisor, "_spawn", lambda self, p, b: None
    )
    monkeypatch.setattr(runner.DeploymentSupervisor, "_supervise", lambda self: None)

    client = FakeBuildClient()
    supervisor = runner.DeploymentSupervisor(
        client,
        {
            "id": "dep-build-heartbeat",
            "agent_name": "slow-build",
            "source_blob_id": "blob-build-heartbeat",
        },
        "workspace-test",
    )

    supervisor.run()

    assert "building" in client.statuses
    assert len(client.heartbeats) >= 2


def test_supervisor_user_stop_terminates_running_bot(
    client, alice, worker_client,
):
    h = auth_headers(alice["session_token"])
    bot_py = (
        "import time, sys\n"
        "print('alive', flush=True)\n"
        "while True:\n"
        "    time.sleep(0.2)\n"
    )
    dep = _upload_bot(
        client, h, "long-bot",
        {"bot.py": bot_py, "requirements.txt": ""},
    )

    claimed = worker_client.claim("worker-test")
    sup = runner.DeploymentSupervisor(
        worker_client, claimed["deployment"], claimed["workspace_id"],
    )
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()

    # Wait for status=running, then flip desired_state in the DB.
    deadline = time.time() + 30
    while time.time() < deadline:
        cur = client.get(
            f"/workspaces/me/deployments/{dep['id']}", headers=h,
        ).json()
        if cur["status"] == "running":
            break
        time.sleep(0.2)
    else:
        pytest.fail("deployment never reached status=running")

    from sqlalchemy import text
    from db import engine
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE deployments SET desired_state = 'stopped' WHERE id = :id"
            ),
            {"id": dep["id"]},
        )

    t.join(timeout=30)
    assert not t.is_alive(), "supervisor did not honor desired_state=stopped"
    final = client.get(
        f"/workspaces/me/deployments/{dep['id']}", headers=h,
    ).json()
    assert final["status"] == "stopped"
