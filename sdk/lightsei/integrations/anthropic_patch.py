"""Anthropic auto-instrumentation.

Patches `anthropic.resources.messages.Messages.create` and the async
equivalent. Same shape as `openai_patch`:
- Idempotent (`_lightsei_patched` marker on the class)
- Skips silently if `anthropic` isn't installed
- Streaming (`stream=True`) passes through, deferred to a later phase
- Calls /policy/check before the underlying create; raises LightseiPolicyError
  on deny
- Emits llm_call_started, llm_call_completed (with model + input/output
  tokens + duration), llm_call_failed
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

logger = logging.getLogger("lightsei.anthropic")

_PATCH_MARKER = "_lightsei_patched"
_ACTION = "anthropic.messages.create"


def patch_anthropic() -> bool:
    try:
        from anthropic.resources.messages import (  # type: ignore
            AsyncMessages,
            Messages,
        )
    except Exception as e:
        logger.debug("anthropic not installed; skipping patch: %s", e)
        return False

    _patch_sync(Messages)
    _patch_async(AsyncMessages)
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
            out["input_tokens"] = getattr(usage, "input_tokens", None)
            out["output_tokens"] = getattr(usage, "output_tokens", None)
            in_t = out.get("input_tokens") or 0
            out_t = out.get("output_tokens") or 0
            out["total_tokens"] = in_t + out_t
    except Exception:
        pass
    return out


def _check_policy_or_raise(req: dict[str, Any]) -> None:
    decision = _client.check_policy(_ACTION, payload=req)
    if not decision.get("allow", True):
        reason = decision.get("reason", "policy denied")
        _client.emit("policy_denied", {"action": _ACTION, **decision})
        raise LightseiPolicyError(reason, decision)


_STREAM_LABEL = "anthropic.messages.create"


def _instrumented_call(original, self, args, kwargs):
    req = _summarize_request(kwargs)
    with implicit_run(_STREAM_LABEL):
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
    async with implicit_run_async(_STREAM_LABEL):
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


def _make_stream_observers(req, started, run_id, is_implicit):
    """Build (on_chunk, on_finish) for Anthropic streams.

    Anthropic emits typed events: message_start carries the initial message
    with `usage.input_tokens`, message_delta carries cumulative
    `usage.output_tokens`, content_block_delta carries text chunks.
    """
    state: dict[str, Any] = {
        "model": req.get("model"),
        "input_tokens": None,
        "output_tokens": None,
        "output_chunks": 0,
    }

    def on_chunk(event: Any) -> None:
        try:
            etype = getattr(event, "type", None)
            if etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    model = getattr(msg, "model", None)
                    if model:
                        state["model"] = model
                    usage = getattr(msg, "usage", None)
                    if usage is not None:
                        in_t = getattr(usage, "input_tokens", None)
                        if in_t is not None:
                            state["input_tokens"] = in_t
                        out_t = getattr(usage, "output_tokens", None)
                        if out_t is not None:
                            state["output_tokens"] = out_t
            elif etype == "message_delta":
                usage = getattr(event, "usage", None)
                if usage is not None:
                    out_t = getattr(usage, "output_tokens", None)
                    if out_t is not None:
                        state["output_tokens"] = out_t
            elif etype == "content_block_delta":
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
