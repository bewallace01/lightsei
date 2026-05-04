"""Google Gemini auto-instrumentation (Phase 12.2).

Patches `google.generativeai.GenerativeModel.generate_content` and the
async equivalent. Same shape as `openai_patch` and `anthropic_patch`:

- Idempotent (`_lightsei_patched` marker on the class).
- Skips silently if `google-generativeai` isn't installed (transparent
  fallback — bots that don't call Gemini don't pay an import cost).
- Streaming (`stream=True` on the call OR `generate_content_stream`)
  passes through, deferred to a later phase.
- Calls /policy/check before the underlying call; raises
  LightseiPolicyError on deny.
- Emits llm_call_started, llm_call_completed (with model + input/output
  tokens + duration), llm_call_failed.

We target the older `google-generativeai` package (the most widely
deployed Python Gemini SDK as of 2026) rather than the newer
`google-genai`. Adding the newer one is a separate try/import block
when there's demand — both can coexist patched simultaneously since
each maintains its own marker.
"""

import functools
import logging
import time
from typing import Any

from .._client import _client
from ..errors import LightseiPolicyError
from ._runscope import implicit_run, implicit_run_async

logger = logging.getLogger("lightsei.gemini")

_PATCH_MARKER = "_lightsei_patched"
_ACTION = "google.generativeai.generate_content"


def patch_gemini() -> bool:
    try:
        from google.generativeai import GenerativeModel  # type: ignore
    except Exception as e:
        logger.debug("google-generativeai not installed; skipping patch: %s", e)
        return False

    _patch_sync(GenerativeModel)
    _patch_async(GenerativeModel)
    return True


def _patch_sync(cls: type) -> None:
    if getattr(cls, _PATCH_MARKER, False):
        return
    original = cls.generate_content

    @functools.wraps(original)
    def wrapped(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            # Streaming pass-through for now; structured stream tap
            # for Gemini lands when 12.2 grows beyond the first slice.
            return original(self, *args, **kwargs)
        return _instrumented_call(original, self, args, kwargs)

    cls.generate_content = wrapped  # type: ignore[assignment]
    setattr(cls, _PATCH_MARKER, True)
    logger.debug("patched %s.generate_content", cls.__name__)


def _patch_async(cls: type) -> None:
    # `generate_content_async` is the coroutine variant. The marker on
    # the class is shared with the sync patch, so we don't re-mark here.
    original = getattr(cls, "generate_content_async", None)
    if original is None:
        return

    @functools.wraps(original)
    async def wrapped(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return await original(self, *args, **kwargs)
        return await _instrumented_call_async(original, self, args, kwargs)

    cls.generate_content_async = wrapped  # type: ignore[assignment]


def _model_name_from(self_obj: Any) -> Any:
    """The user constructs `GenerativeModel('models/gemini-1.5-flash')`
    or `GenerativeModel('gemini-1.5-flash')`. Both shapes get normalized
    to the bare model id so it lines up with PRICING keys + the cost
    panel's per-model row in the dashboard.
    """
    raw = getattr(self_obj, "model_name", None) or getattr(self_obj, "_model_name", None)
    if not isinstance(raw, str):
        return raw
    if raw.startswith("models/"):
        return raw[len("models/"):]
    return raw


def _summarize_request(self_obj: Any, args: tuple, kwargs: dict[str, Any]) -> dict[str, Any]:
    # `contents` is positional first arg (or kwarg). Like Anthropic's
    # messages, can be a string or a list of dicts. Keep it flexible.
    contents = kwargs.get("contents")
    if contents is None and args:
        contents = args[0]
    if isinstance(contents, list):
        msg_count = len(contents)
    elif isinstance(contents, str):
        msg_count = 1
    else:
        msg_count = None
    out: dict[str, Any] = {
        "model": _model_name_from(self_obj),
        "message_count": msg_count,
    }
    if _client.capture_content and contents is not None:
        if isinstance(contents, list):
            out["request_messages"] = [
                dict(m) if isinstance(m, dict) else m for m in contents
            ]
        elif isinstance(contents, str):
            out["request_messages"] = [{"role": "user", "content": contents}]
    return out


def _summarize_response(resp: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            in_t = getattr(usage, "prompt_token_count", None)
            out_t = getattr(usage, "candidates_token_count", None)
            total = getattr(usage, "total_token_count", None)
            if in_t is not None:
                out["input_tokens"] = in_t
            if out_t is not None:
                out["output_tokens"] = out_t
            if total is not None:
                out["total_tokens"] = total
            elif in_t is not None and out_t is not None:
                out["total_tokens"] = in_t + out_t
    except Exception:
        pass
    if _client.capture_content:
        try:
            text = getattr(resp, "text", None)
            if isinstance(text, str) and text:
                out["response_content"] = text
        except Exception:
            # `.text` raises if response was blocked / empty — swallow
            # so the event still emits the token + model fields.
            pass
    return out


def _check_policy_or_raise(req: dict[str, Any]) -> None:
    decision = _client.check_policy(_ACTION, payload=req)
    if not decision.get("allow", True):
        reason = decision.get("reason", "policy denied")
        denied_payload: dict[str, Any] = {"action": _ACTION, **decision}
        if "request_messages" in req:
            denied_payload["request_messages"] = req["request_messages"]
        if "model" in req:
            denied_payload["model"] = req["model"]
        _client.emit("policy_denied", denied_payload)
        raise LightseiPolicyError(reason, decision)


_RUN_LABEL = "google.generativeai.generate_content"


def _instrumented_call(original, self, args, kwargs):
    req = _summarize_request(self, args, kwargs)
    with implicit_run(_RUN_LABEL):
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
    req = _summarize_request(self, args, kwargs)
    async with implicit_run_async(_RUN_LABEL):
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
