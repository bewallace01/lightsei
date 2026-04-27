import http.server
import json
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import pytest

import lightsei
from lightsei._client import _client
from lightsei._secrets import _reset_cache_for_tests as _reset_secret_cache


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    _client._reset_for_tests()
    _reset_secret_cache()


@contextmanager
def fake_backend(
    received: list[dict],
    *,
    secrets: dict[str, str] | None = None,
) -> Iterator[str]:
    secrets = secrets or {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/workspaces/me/secrets/"):
                name = self.path.rsplit("/", 1)[-1]
                if name in secrets:
                    body = json.dumps({"name": name, "value": secrets[name]}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}

            if self.path == "/events":
                received.append(data)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":1,"status":"ok"}')
            elif self.path == "/policy/check":
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"allow":true}')
            elif self.path.endswith("/instances/heartbeat"):
                received.append({"_path": self.path, **data})
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"active"}')
            else:
                self.send_error(404)

        def log_message(self, *_args, **_kwargs):  # silence stderr
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_init_is_idempotent():
    lightsei.init(api_key="k1", agent_name="first", base_url="http://127.0.0.1:1")
    lightsei.init(api_key="k2", agent_name="second", base_url="http://127.0.0.1:1")
    assert _client.agent_name == "first"
    assert _client.api_key == "k1"


def test_emit_and_flush_against_fake_backend():
    received: list[dict] = []
    with fake_backend(received) as url:
        lightsei.init(
            api_key="k", agent_name="demo", base_url=url, flush_interval=0.1,
        )

        @lightsei.track
        def do_work():
            lightsei.emit("custom", {"x": 1})
            return "ok"

        assert do_work() == "ok"
        lightsei.flush(timeout=2.0)
        # give background thread one more tick
        time.sleep(0.2)

    events = [e for e in received if "kind" in e]
    kinds = [e["kind"] for e in events]
    assert "run_started" in kinds
    assert "run_ended" in kinds
    assert "custom" in kinds

    custom = next(e for e in events if e["kind"] == "custom")
    assert custom["payload"] == {"x": 1}
    assert custom["agent_name"] == "demo"


def test_run_completes_with_backend_offline():
    # 127.0.0.1:1 has nothing listening; connections fail fast
    lightsei.init(
        api_key="k",
        agent_name="demo",
        base_url="http://127.0.0.1:1",
        flush_interval=0.05,
        timeout=0.2,
        max_retries=2,
    )

    @lightsei.track
    def do_work():
        lightsei.emit("custom", {"x": 1})
        return "ok"

    # User code must keep running even though every send will fail.
    assert do_work() == "ok"
    # flush must not raise either
    lightsei.flush(timeout=0.5)


def test_emit_before_init_is_silent():
    # No init. emit() must not raise and must not connect.
    lightsei.emit("anything", {"x": 1})


def test_heartbeat_registers_on_init():
    """init() should fire a synchronous heartbeat so the dashboard sees the
    instance immediately, without waiting for the first timer tick."""
    received: list[dict] = []
    with fake_backend(received) as url:
        lightsei.init(
            api_key="k",
            agent_name="demo",
            base_url=url,
            heartbeat_interval=10.0,  # we only care about the synchronous one
        )
        # Give the eager post a moment to land.
        time.sleep(0.1)

    heartbeats = [r for r in received if r.get("_path", "").endswith("/heartbeat")]
    assert heartbeats, "expected a heartbeat post on init"
    h = heartbeats[0]
    assert h["instance_id"]
    assert h["pid"]
    assert h["hostname"]
    assert h["_path"] == "/agents/demo/instances/heartbeat"


def test_get_secret_fetches_and_caches():
    received: list[dict] = []
    secrets = {"OPENAI_API_KEY": "sk-pretend"}
    with fake_backend(received, secrets=secrets) as url:
        lightsei.init(api_key="k", agent_name="demo", base_url=url)

        # Block the heartbeat thread from racing with the test by tearing down
        # the fake backend's view of "received" — heartbeats POST and won't
        # show up in our GETs. So just exercise the secret fetch.
        v1 = lightsei.get_secret("OPENAI_API_KEY")
        assert v1 == "sk-pretend"

        # Mutate the server-side value; cached call should not see it.
        secrets["OPENAI_API_KEY"] = "sk-changed"
        v2 = lightsei.get_secret("OPENAI_API_KEY")
        assert v2 == "sk-pretend"

        # ttl_s=0 forces a refetch.
        v3 = lightsei.get_secret("OPENAI_API_KEY", ttl_s=0)
        assert v3 == "sk-changed"


def test_get_secret_404_raises_clear_error():
    with fake_backend([]) as url:
        lightsei.init(api_key="k", agent_name="demo", base_url=url)
        with pytest.raises(lightsei.LightseiError) as exc:
            lightsei.get_secret("MISSING")
        assert "not set" in str(exc.value)


def test_get_secret_before_init_raises():
    with pytest.raises(lightsei.LightseiError):
        lightsei.get_secret("ANYTHING")


def test_policy_check_fails_open_when_offline():
    lightsei.init(
        api_key="k",
        agent_name="demo",
        base_url="http://127.0.0.1:1",
        timeout=0.2,
    )
    decision = lightsei.check_policy("openai.chat.completions.create")
    assert decision == {"allow": True}
