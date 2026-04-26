"""OpenAI auto-instrumentation.

Patches `openai.resources.chat.completions.Completions.create` and the async
equivalent. Idempotent: marks each class with `_lightsei_patched = True` so a
second call is a no-op. Skips silently if `openai` is not installed.

Streaming (`stream=True`) is deferred to a later phase: those calls pass
through with no instrumentation and no policy check.
"""

import functools
import logging
import time
from typing import Any

from .._client import _client
from .._context import get_run_id
from ..errors import LightseiPolicyError
from ._runscope import (
    close_implicit_run,
    implicit_run,
    implicit_run_async,
    open_implicit_run,
)
from ._streamtap import _AsyncStreamTap, _SyncStreamTap

logger = logging.getLogger("lightsei.openai")

_PATCH_MARKER = "_lightsei_patched"
_ACTION = "openai.chat.completions.create"


def patch_openai() -> bool:
    """Patch openai chat.completions.create. Returns True if patched (or already
    patched), False if openai isn't installed."""
    try:
        from openai.resources.chat.completions import (  # type: ignore
            AsyncCompletions,
            Completions,
        )
    except Exception as e:
        logger.debug("openai not installed; skipping patch: %s", e)
        return False

    _patch_sync(Completions)
    _patch_async(AsyncCompletions)
    return True


def _patch_sync(cls: type) -> None:
    if getattr(cls, _PATCH_MARKER, False):
        return
    original = cls.create

    @functools.wraps(original)
    def wrapped(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return _instrumented_stream(original, self, args, kwargs)
        return _instrumented_call(original, self, args, kwargs)

    cls.create = wrapped  # type: ignore[assignment]
    setattr(cls, _PATCH_MARKER, True)
    logger.debug("patched %s.create", cls.__name__)


def _patch_async(cls: type) -> None:
    if getattr(cls, _PATCH_MARKER, False):
        return
    original = cls.create

    @functools.wraps(original)
    async def wrapped(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return await _instrumented_stream_async(original, self, args, kwargs)
        return await _instrumented_call_async(original, self, args, kwargs)

    cls.create = wrapped  # type: ignore[assignment]
    setattr(cls, _PATCH_MARKER, True)
    logger.debug("patched %s.create", cls.__name__)


def _summarize_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    messages = kwargs.get("messages")
    msg_count = len(messages) if isinstance(messages, list) else None
    return {
        "model": kwargs.get("model"),
        "message_count": msg_count,
    }


def _summarize_response(resp: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        model = getattr(resp, "model", None)
        if model is not None:
            out["model"] = model
    except Exception:
        pass
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            out["input_tokens"] = getattr(usage, "prompt_tokens", None)
            out["output_tokens"] = getattr(usage, "completion_tokens", None)
            out["total_tokens"] = getattr(usage, "total_tokens", None)
    except Exception:
        pass
    return out


def _check_policy_or_raise(req: dict[str, Any]) -> None:
    decision = _client.check_policy(_ACTION, payload=req)
    if not decision.get("allow", True):
        reason = decision.get("reason", "policy denied")
        # Record the denial as an event so it shows up in the dashboard.
        _client.emit("policy_denied", {"action": _ACTION, **decision})
        raise LightseiPolicyError(reason, decision)


def _instrumented_call(original, self, args, kwargs):
    req = _summarize_request(kwargs)
    with implicit_run("openai.chat.completions.create"):
        _check_policy_or_raise(req)
        _client.emit("llm_call_started", req)
        started = time.time()
        try:
            result = original(self, *args, **kwargs)
        except Exception as e:
            _client.emit(
                "llm_call_failed",
                {**req, "duration_s": time.time() - started, "error": repr(e)},
            )
            raise
        _client.emit(
            "llm_call_completed",
            {**req, **_summarize_response(result), "duration_s": time.time() - started},
        )
        return result


async def _instrumented_call_async(original, self, args, kwargs):
    req = _summarize_request(kwargs)
    async with implicit_run_async("openai.chat.completions.create"):
        _check_policy_or_raise(req)
        _client.emit("llm_call_started", req)
        started = time.time()
        try:
            result = await original(self, *args, **kwargs)
        except Exception as e:
            _client.emit(
                "llm_call_failed",
                {**req, "duration_s": time.time() - started, "error": repr(e)},
            )
            raise
        _client.emit(
            "llm_call_completed",
            {**req, **_summarize_response(result), "duration_s": time.time() - started},
        )
        return result


_STREAM_LABEL = "openai.chat.completions.create"


def _make_stream_observers(req, started, run_id, is_implicit):
    """Build (on_chunk, on_finish) for OpenAI streams.

    We don't auto-inject `stream_options.include_usage` because that would add
    a final empty-choices chunk to the user's iteration. If the user opts in
    to `include_usage`, we capture the resulting usage chunk; otherwise the
    completion event lands without token counts.
    """
    state: dict[str, Any] = {
        "model": req.get("model"),
        "input_tokens": None,
        "output_tokens": None,
        "output_chunks": 0,
    }

    def on_chunk(chunk: Any) -> None:
        try:
            model = getattr(chunk, "model", None)
            if model:
                state["model"] = model
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                state["input_tokens"] = getattr(usage, "prompt_tokens", state["input_tokens"])
                state["output_tokens"] = getattr(usage, "completion_tokens", state["output_tokens"])
            choices = getattr(chunk, "choices", None) or []
            for c in choices:
                delta = getattr(c, "delta", None)
                if delta is not None and getattr(delta, "content", None):
                    state["output_chunks"] += 1
        except Exception:
            pass

    def on_finish() -> None:
        payload: dict[str, Any] = {
            **req,
            "model": state["model"],
            "duration_s": time.time() - started,
            "stream": True,
            "output_chunks": state["output_chunks"],
        }
        if state["input_tokens"] is not None:
            payload["input_tokens"] = state["input_tokens"]
        if state["output_tokens"] is not None:
            payload["output_tokens"] = state["output_tokens"]
        _client.emit("llm_call_completed", payload, run_id=run_id)
        close_implicit_run(run_id, is_implicit, _STREAM_LABEL)

    return on_chunk, on_finish


def _instrumented_stream(original, self, args, kwargs):
    req = _summarize_request(kwargs)
    _check_policy_or_raise(req)
    run_id, is_implicit = open_implicit_run(_STREAM_LABEL)
    _client.emit("llm_call_started", {**req, "stream": True}, run_id=run_id)
    started = time.time()
    try:
        stream = original(self, *args, **kwargs)
    except Exception as e:
        _client.emit(
            "llm_call_failed",
            {**req, "duration_s": time.time() - started, "error": repr(e), "stream": True},
            run_id=run_id,
        )
        close_implicit_run(run_id, is_implicit, _STREAM_LABEL, error=e)
        raise
    on_chunk, on_finish = _make_stream_observers(req, started, run_id, is_implicit)
    return _SyncStreamTap(stream, on_chunk, on_finish)


async def _instrumented_stream_async(original, self, args, kwargs):
    req = _summarize_request(kwargs)
    _check_policy_or_raise(req)
    run_id, is_implicit = open_implicit_run(_STREAM_LABEL)
    _client.emit("llm_call_started", {**req, "stream": True}, run_id=run_id)
    started = time.time()
    try:
        stream = await original(self, *args, **kwargs)
    except Exception as e:
        _client.emit(
            "llm_call_failed",
            {**req, "duration_s": time.time() - started, "error": repr(e), "stream": True},
            run_id=run_id,
        )
        close_implicit_run(run_id, is_implicit, _STREAM_LABEL, error=e)
        raise
    on_chunk, on_finish = _make_stream_observers(req, started, run_id, is_implicit)
    return _AsyncStreamTap(stream, on_chunk, on_finish)
