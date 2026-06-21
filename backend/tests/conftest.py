"""Test infrastructure for the backend.

How it works:
  1. At conftest import time (before any backend module is imported), pick a
     test database URL. If LIGHTSEI_TEST_DATABASE_URL is set we use it;
     otherwise we spawn a throwaway postgres:18-alpine docker container on
     a free local port.
  2. Set LIGHTSEI_DATABASE_URL to that URL so backend modules pick it up
     when first imported.
  3. Import the FastAPI app (which triggers alembic upgrade on startup).
  4. Per-test, truncate every data table so tests don't see each other's rows.
     We deliberately don't touch alembic_version, so migrations stay applied.

Per project preference: tests hit a real Postgres, never a mock. The mock vs
real divergence has bitten us before (alembic env.py psycopg2 vs psycopg3
silent restart-loop). Real DB tests would have caught that on the first run.
"""
import atexit
import os
import socket
import subprocess
import time
from typing import Iterator

import pytest

_OWNED_CONTAINER_ID: str | None = None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_tcp(host: str, port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _spawn_pg() -> tuple[str, str]:
    port = _free_port()
    cid = subprocess.check_output(
        [
            "docker", "run", "-d", "--rm",
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_USER=test",
            "-e", "POSTGRES_DB=test",
            "-p", f"{port}:5432",
            "postgres:18-alpine",
        ],
        stderr=subprocess.STDOUT,
    ).decode().strip()
    if not _wait_for_tcp("127.0.0.1", port, timeout_s=30.0):
        subprocess.run(["docker", "stop", cid], check=False, capture_output=True)
        raise RuntimeError(f"postgres test container {cid[:12]} did not become reachable")
    # Wait a beat after TCP open; pg's listener accepts before it's ready.
    time.sleep(1.5)
    url = f"postgresql+psycopg://test:test@127.0.0.1:{port}/test"
    return cid, url


def _teardown_owned_pg() -> None:
    global _OWNED_CONTAINER_ID
    if _OWNED_CONTAINER_ID:
        subprocess.run(
            ["docker", "stop", _OWNED_CONTAINER_ID],
            check=False, capture_output=True,
        )
        _OWNED_CONTAINER_ID = None


# Resolve the test DB URL eagerly so it's set before db.py / main.py import.
_test_url = os.environ.get("LIGHTSEI_TEST_DATABASE_URL")
if not _test_url:
    _OWNED_CONTAINER_ID, _test_url = _spawn_pg()
    atexit.register(_teardown_owned_pg)
os.environ["LIGHTSEI_DATABASE_URL"] = _test_url

# Provide a deterministic master key so secrets_crypto is "available" in tests.
# Override only when not already set so an opinionated test suite (e.g.
# verifying the 503 fail-closed path) can choose its own value.
os.environ.setdefault(
    "LIGHTSEI_SECRETS_KEY",
    # 32 bytes of zeroes, base64-encoded. Fine for tests; never use in prod.
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
)
# Same idea for the worker shared secret.
os.environ.setdefault("LIGHTSEI_WORKER_TOKEN", "test-worker-token")

# Keep the app's startup from spawning the PERIODIC background workers (eval
# cron + trigger scheduler). Those poll the same test DB on a timer and drop
# new jobs/runs that race tests asserting on job/trigger state — the source of
# the intermittent test_eval_runner flake. The jobs RUNNER stays on (many tests
# enqueue a row and wait for it to be processed); see main.on_startup.
os.environ.setdefault("LIGHTSEI_DISABLE_BACKGROUND", "1")


# Now safe to import backend modules.
from fastapi.testclient import TestClient  # noqa: E402

from db import engine  # noqa: E402
import jobs  # noqa: E402
from limits import reset_counter_for_tests  # noqa: E402
from main import app  # noqa: E402
from migrate import upgrade_to_head  # noqa: E402


# Phase 12C.6: shrink the runner's idle sleep so async-generation tests
# don't pay the default 500ms gap between claim attempts. Worth ~10s
# saved across the suite once endpoints all use the kick-off path.
jobs._IDLE_SLEEP_S = 0.01

@pytest.fixture(scope="session", autouse=True)
def _migrate_schema() -> list[str]:
    """Run alembic upgrade once per session, then snapshot the data tables.
    Returning the list lets _truncate_between_tests reuse it without
    re-querying information_schema on every test."""
    upgrade_to_head()
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name <> 'alembic_version'
                """
            )
        ).all()
    return [r[0] for r in rows]


@pytest.fixture(autouse=True)
def _truncate_between_tests(_migrate_schema) -> Iterator[None]:
    yield
    reset_counter_for_tests()
    if not _migrate_schema:
        return
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(
            text(
                f"TRUNCATE TABLE {', '.join(_migrate_schema)} "
                f"RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------- helpers ---------- #

def signup(client: TestClient, email: str = "alice@example.com",
           password: str = "hunter22hunter22",
           workspace_name: str = "alice-co") -> dict:
    r = client.post(
        "/auth/signup",
        json={"email": email, "password": password, "workspace_name": workspace_name},
    )
    assert r.status_code == 200, r.text
    return r.json()


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------- Phase 12C.6 async-generation helpers ---------- #


class GenerationJobFailed(AssertionError):
    """Raised by `kick_and_wait_for_job` when the runner finalizes the
    job as failed. Carries the error text and the full row so a test
    asserting on a specific failure mode can introspect either."""

    def __init__(self, error: str | None, row: dict):
        super().__init__(error or "generation job failed without error text")
        self.error = error or ""
        self.row = row


def kick_and_wait_for_job(
    client: TestClient,
    headers: dict[str, str],
    *,
    path: str,
    body: dict,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.02,
) -> dict:
    """POST a generation-job kick-off, wait for the runner, return the result.

    The 12C.6 refactor moved /agents/generate and /teams/plan onto an
    async job queue. Both endpoints now return 202 + {job_id}; the
    in-process runner picks up the row and writes result_payload (on
    success) or error (on failure). This helper hides that loop so
    endpoint tests still look mostly like "call POST, check the result."

    On `success`: returns the `result_payload` dict (what the old
    synchronous response body used to be, modulo the job-id wrapper).
    On `failed`: raises `GenerationJobFailed` carrying the error text;
    tests use `with pytest.raises(GenerationJobFailed) as exc` and
    assert on `exc.value.error`.

    On non-202 status (4xx pre-enqueue gates: missing input, missing
    secret, over budget): raises AssertionError so the test knows to
    call `client.post` directly instead of going through the helper.
    """
    import time as _time
    r = client.post(path, headers=headers, json=body)
    assert r.status_code == 202, (
        f"expected 202 from {path}, got {r.status_code}: {r.text}"
    )
    job_id = r.json()["job_id"]
    deadline = _time.monotonic() + timeout_s
    while True:
        poll = client.get(
            f"/workspaces/me/generation-jobs/{job_id}",
            headers=headers,
        )
        assert poll.status_code == 200, poll.text
        row = poll.json()
        status = row["status"]
        if status == "success":
            return row["result_payload"]
        if status == "failed":
            raise GenerationJobFailed(row["error"], row=row)
        if _time.monotonic() > deadline:
            raise AssertionError(
                f"job {job_id} still {status} after {timeout_s}s; row={row}"
            )
        _time.sleep(poll_interval_s)


@pytest.fixture()
def alice(client: TestClient) -> dict:
    """A fresh workspace + user + api key + session token for the test."""
    return signup(client, email="alice@example.com", workspace_name="alice-co")


@pytest.fixture()
def bob(client: TestClient) -> dict:
    return signup(client, email="bob@example.com", workspace_name="bob-co")
