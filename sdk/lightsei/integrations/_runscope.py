"""Implicit-run scoping for auto-instrumented calls.

When a user calls an instrumented method (e.g. `client.chat.completions.create`)
without wrapping their function in `@lightsei.track`, there is no run_id in
context and every emit() drops on the floor. That's surprising — most users
expect the call itself to show up.

`implicit_run` solves this by giving the patch a sync/async context manager:
- if a run_id is already set (because we're inside @lightsei.track), it does
  nothing and yields the existing run_id.
- otherwise it generates a fresh run_id, emits run_started, yields, and on
  exit emits run_ended (or run_failed on exception). The contextvar is reset
  on exit either way.
"""
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from .._client import _client
from .._context import _reset_run_id, _set_run_id, get_run_id


@contextmanager
def implicit_run(label: str):
    """Sync version. `label` is recorded as the function name for display."""
    existing = get_run_id()
    if existing is not None:
        yield existing
        return
    run_id = str(uuid.uuid4())
    token = _set_run_id(run_id)
    _client.emit("run_started", {"function": label, "implicit": True}, run_id=run_id)
    try:
        yield run_id
    except BaseException as e:
        _client.emit(
            "run_failed",
            {"function": label, "error": repr(e), "implicit": True},
            run_id=run_id,
        )
        raise
    else:
        _client.emit("run_ended", {"function": label, "implicit": True}, run_id=run_id)
    finally:
        _reset_run_id(token)


@asynccontextmanager
async def implicit_run_async(label: str):
    existing = get_run_id()
    if existing is not None:
        yield existing
        return
    run_id = str(uuid.uuid4())
    token = _set_run_id(run_id)
    _client.emit("run_started", {"function": label, "implicit": True}, run_id=run_id)
    try:
        yield run_id
    except BaseException as e:
        _client.emit(
            "run_failed",
            {"function": label, "error": repr(e), "implicit": True},
            run_id=run_id,
        )
        raise
    else:
        _client.emit("run_ended", {"function": label, "implicit": True}, run_id=run_id)
    finally:
        _reset_run_id(token)


def open_implicit_run(label: str) -> tuple[str, bool]:
    """For streams, where the run's lifetime spans an async iterator and we
    can't use a context manager. Returns (run_id, is_implicit).

    If a run is already in context, returns it untouched. Otherwise creates a
    new run, emits run_started, and returns its id with is_implicit=True. The
    caller MUST eventually call `close_implicit_run` to end it.
    """
    existing = get_run_id()
    if existing is not None:
        return existing, False
    run_id = str(uuid.uuid4())
    _client.emit("run_started", {"function": label, "implicit": True}, run_id=run_id)
    return run_id, True


def close_implicit_run(
    run_id: str,
    is_implicit: bool,
    label: str,
    error: Optional[BaseException] = None,
) -> None:
    if not is_implicit:
        return
    if error is not None:
        _client.emit(
            "run_failed",
            {"function": label, "error": repr(error), "implicit": True},
            run_id=run_id,
        )
    else:
        _client.emit("run_ended", {"function": label, "implicit": True}, run_id=run_id)
