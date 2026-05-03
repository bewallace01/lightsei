"""Phase 11.1: SDK send_command / claim_command / complete_command.

Covers the public dispatch surface (the explicit-control flow) and the
thread-local chain-id propagation that lets a chain like
`polaris -> atlas -> hermes` group correctly without the caller threading
the chain id through call signatures.

Auto-poller behavior (the @on_command decorator) is exercised in the
backend test suite end-to-end; here we test the dispatch primitives in
isolation against an in-process fake backend so failures point at the
SDK, not at the backend or wire format.
"""
import http.server
import json
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

import lightsei
from lightsei._client import _client
from lightsei._commands import (
    _clear_dispatch_context,
    _command_context,
    _handlers,
    current_dispatch_chain_id,
)


@pytest.fixture(autouse=True)
def _reset_client():
    # The built-in `ping` handler makes `has_handlers()` always True, which
    # would start the auto-poller in `init()` and race our manual
    # `claim_command` calls (whichever wins consumes the seeded command).
    # Stash and clear the handler registry for the duration of each test;
    # restore on teardown so we don't poison sibling test files.
    saved = dict(_handlers)
    _handlers.clear()
    try:
        yield
    finally:
        _client._reset_for_tests()
        _handlers.update(saved)
        # Tests run on the main thread; clear any leaked context so a
        # failed test doesn't poison the next one.
        _clear_dispatch_context()


@contextmanager
def fake_command_backend(
    *,
    queued: list[dict] | None = None,
) -> Iterator[tuple[str, dict]]:
    """Fake `/agents/{name}/commands*` endpoints.

    Returns the base URL plus a state dict the test can read:
      enqueued: list of bodies POSTed to enqueue (in order)
      claimed:  list of command dicts returned by claim
      completed: list of (cmd_id, body) tuples POSTed to complete

    `queued` is a list of pre-seeded commands the claim endpoint pops from
    in FIFO order. When empty, claim returns {"command": null}.
    """
    state = {
        "enqueued": [],
        "claimed": [],
        "completed": [],
        "next_id": 1,
        "queue": list(queued or []),
    }
    state_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _read_json(self) -> dict:
            length = int(self.headers.get("content-length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}

        def _respond(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):  # noqa: N802
            data = self._read_json()
            with state_lock:
                # POST /agents/{name}/commands
                if (
                    self.path.startswith("/agents/")
                    and self.path.endswith("/commands")
                ):
                    target = self.path.split("/")[2]
                    cmd_id = f"cmd-{state['next_id']}"
                    state["next_id"] += 1
                    cmd = {
                        "id": cmd_id,
                        "agent_name": target,
                        "kind": data.get("kind"),
                        "payload": data.get("payload") or {},
                        # The real backend in 11.1 silently drops this
                        # field; we echo it back so SDK tests can verify
                        # propagation now and the same tests will keep
                        # working when 11.2 starts persisting it.
                        "dispatch_chain_id": data.get("dispatch_chain_id"),
                        "status": "pending",
                    }
                    state["enqueued"].append(data)
                    self._respond(200, cmd)
                    return

                # POST /agents/{name}/commands/claim
                if self.path.endswith("/commands/claim"):
                    if state["queue"]:
                        cmd = state["queue"].pop(0)
                        state["claimed"].append(cmd)
                        self._respond(200, {"command": cmd})
                    else:
                        self._respond(200, {"command": None})
                    return

                # POST /commands/{id}/complete
                if self.path.startswith("/commands/") and self.path.endswith(
                    "/complete"
                ):
                    cmd_id = self.path.split("/")[2]
                    state["completed"].append((cmd_id, data))
                    self._respond(
                        200,
                        {"id": cmd_id, "status": "completed", **data},
                    )
                    return

            self.send_error(404)

        def log_message(self, *_args, **_kwargs):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        server.server_close()


def test_send_command_round_trips():
    with fake_command_backend() as (url, state):
        lightsei.init(api_key="k", agent_name="polaris", base_url=url)
        cmd = lightsei.send_command(
            "atlas", "atlas.run_tests", {"commit": "abc123"}
        )
    assert cmd["agent_name"] == "atlas"
    assert cmd["kind"] == "atlas.run_tests"
    assert cmd["payload"] == {"commit": "abc123"}
    assert cmd["dispatch_chain_id"]  # auto-generated
    # Server received the chain id on the wire even though it doesn't
    # persist it yet — forward-compat for 11.2.
    assert state["enqueued"][0]["dispatch_chain_id"] == cmd["dispatch_chain_id"]


def test_send_command_explicit_chain_id_overrides_inheritance():
    explicit = "00000000-0000-4000-8000-000000000000"
    with fake_command_backend() as (url, state):
        lightsei.init(api_key="k", agent_name="polaris", base_url=url)
        cmd = lightsei.send_command(
            "atlas", "atlas.run_tests", {}, dispatch_chain_id=explicit
        )
    assert cmd["dispatch_chain_id"] == explicit
    assert state["enqueued"][0]["dispatch_chain_id"] == explicit


def test_claim_then_send_inherits_chain_id():
    """The whole point of the thread-local context: a handler that claims
    a command and then dispatches a follow-up should join the same chain
    without the caller threading the id through manually."""
    seeded = {
        "id": "cmd-from-polaris",
        "agent_name": "atlas",
        "kind": "atlas.run_tests",
        "payload": {"commit": "abc"},
        "dispatch_chain_id": "chain-of-the-day",
        "status": "claimed",
    }
    with fake_command_backend(queued=[seeded]) as (url, state):
        lightsei.init(api_key="k", agent_name="atlas", base_url=url)
        claimed = lightsei.claim_command()
        assert claimed is not None
        assert claimed["id"] == "cmd-from-polaris"
        # Inside the claim, the context should be live.
        assert current_dispatch_chain_id() == "chain-of-the-day"
        # Dispatch a follow-up — chain id must inherit.
        followup = lightsei.send_command("hermes", "hermes.post", {"text": "ok"})
        assert followup["dispatch_chain_id"] == "chain-of-the-day"
        lightsei.complete_command(claimed["id"], result={"ok": True})
    # Context cleared after complete.
    assert current_dispatch_chain_id() is None
    assert state["completed"][0][1] == {"result": {"ok": True}}


def test_claim_returns_none_on_empty_queue():
    with fake_command_backend(queued=[]) as (url, _state):
        lightsei.init(api_key="k", agent_name="atlas", base_url=url)
        cmd = lightsei.claim_command()
    assert cmd is None
    # No active claim means no inherited context.
    assert current_dispatch_chain_id() is None


def test_complete_with_error_clears_context():
    seeded = {
        "id": "cmd-1",
        "agent_name": "atlas",
        "kind": "atlas.run_tests",
        "payload": {},
        "dispatch_chain_id": "chain-1",
        "status": "claimed",
    }
    with fake_command_backend(queued=[seeded]) as (url, state):
        lightsei.init(api_key="k", agent_name="atlas", base_url=url)
        claim = lightsei.claim_command()
        assert claim is not None
        lightsei.complete_command(claim["id"], error="boom")
    assert current_dispatch_chain_id() is None
    assert state["completed"][0][1] == {"error": "boom"}


def test_complete_prefers_error_over_result_when_both_passed():
    """Defensive contract: passing both is ambiguous user intent. The
    explicit error wins because that's what most callers actually mean
    when they accidentally send both — they hit a failure path that
    happens to also have a partial result."""
    seeded = {
        "id": "cmd-1",
        "agent_name": "atlas",
        "kind": "atlas.run_tests",
        "payload": {},
        "dispatch_chain_id": "chain-1",
        "status": "claimed",
    }
    with fake_command_backend(queued=[seeded]) as (url, state):
        lightsei.init(api_key="k", agent_name="atlas", base_url=url)
        cmd = lightsei.claim_command()
        assert cmd is not None
        lightsei.complete_command(
            cmd["id"], result={"partial": True}, error="boom"
        )
    assert state["completed"][0][1] == {"error": "boom"}


def test_send_command_without_init_raises():
    # _client._reset_for_tests() in the autouse fixture leaves the SDK
    # un-init'd, so this exercises the pre-init guard.
    with pytest.raises(lightsei.LightseiError):
        lightsei.send_command("atlas", "x.y", {})


def test_claim_command_without_agent_name_raises():
    with fake_command_backend() as (url, _state):
        lightsei.init(api_key="k", base_url=url)  # no agent_name
        with pytest.raises(ValueError):
            lightsei.claim_command()


def test_claim_explicit_agent_name_overrides_init_default():
    seeded = {
        "id": "cmd-x",
        "agent_name": "elsewhere",
        "kind": "x.y",
        "payload": {},
        "dispatch_chain_id": "chain-x",
        "status": "claimed",
    }
    with fake_command_backend(queued=[seeded]) as (url, state):
        lightsei.init(api_key="k", agent_name="polaris", base_url=url)
        cmd = lightsei.claim_command(agent_name="elsewhere")
    assert cmd is not None
    assert cmd["id"] == "cmd-x"


def test_dispatch_context_is_thread_local():
    """Two concurrent claims should not see each other's chain ids."""
    seeded_a = {
        "id": "cmd-a",
        "agent_name": "atlas",
        "kind": "x.y",
        "payload": {},
        "dispatch_chain_id": "chain-A",
        "status": "claimed",
    }
    seeded_b = {
        "id": "cmd-b",
        "agent_name": "atlas",
        "kind": "x.y",
        "payload": {},
        "dispatch_chain_id": "chain-B",
        "status": "claimed",
    }
    with fake_command_backend(queued=[seeded_a, seeded_b]) as (url, _state):
        lightsei.init(api_key="k", agent_name="atlas", base_url=url)

        results: dict[str, str | None] = {}
        ready = threading.Event()
        proceed = threading.Event()

        def thread_b():
            cmd = lightsei.claim_command()
            ready.set()
            # Wait until the main thread has also claimed + checked its
            # context; this proves they don't interfere.
            proceed.wait(timeout=2.0)
            results["b"] = current_dispatch_chain_id()
            if cmd is not None:
                lightsei.complete_command(cmd["id"], result={"ok": True})

        t = threading.Thread(target=thread_b)
        t.start()
        ready.wait(timeout=2.0)

        cmd_a = lightsei.claim_command()
        results["a"] = current_dispatch_chain_id()
        proceed.set()
        if cmd_a is not None:
            lightsei.complete_command(cmd_a["id"], result={"ok": True})
        t.join(timeout=2.0)

    # Each thread saw its own chain id, not the other's.
    assert {results["a"], results["b"]} == {"chain-A", "chain-B"}
    # And by now both contexts are cleared.
    assert current_dispatch_chain_id() is None
