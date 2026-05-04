"""Phase 12.2: Gemini auto-patch.

We don't depend on google-generativeai in the SDK's test env (it's a
heavy import we don't want to take just for unit tests). The tests
here build a tiny fake `google.generativeai` module + GenerativeModel
class, register it in sys.modules, then drive `patch_gemini()`
against it. Same pattern Atlas's test suite uses for `lightsei`.
"""
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

import lightsei
from lightsei._client import _client


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    _client._reset_for_tests()


def _install_fake_gemini(monkeypatch):
    """Stub a minimal google.generativeai module with a patchable
    GenerativeModel class. Returns the (module, class) so tests can
    drive the class's `generate_content` directly and assert the
    patch's emitted events on `_client.emit`.
    """

    class FakeUsageMetadata:
        def __init__(self, prompt: int, output: int) -> None:
            self.prompt_token_count = prompt
            self.candidates_token_count = output
            self.total_token_count = prompt + output

    class FakeResponse:
        def __init__(self, text: str, prompt: int, output: int) -> None:
            self.text = text
            self.usage_metadata = FakeUsageMetadata(prompt, output)

    class FakeGenerativeModel:
        def __init__(self, model_name: str) -> None:
            # Match the real SDK shape — `model_name` is the public
            # attribute, sometimes prefixed with "models/".
            self.model_name = model_name

        def generate_content(self, contents: Any, **kwargs: Any) -> Any:
            return FakeResponse("hello from gemini", prompt=12, output=7)

        async def generate_content_async(
            self, contents: Any, **kwargs: Any
        ) -> Any:
            return FakeResponse("hello (async)", prompt=20, output=4)

    fake_module = types.ModuleType("google.generativeai")
    fake_module.GenerativeModel = FakeGenerativeModel  # type: ignore[attr-defined]

    google_pkg = types.ModuleType("google")
    google_pkg.generativeai = fake_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_module)
    return fake_module, FakeGenerativeModel


# ---------- patch_gemini import-skip behavior ---------- #


def test_patch_gemini_returns_false_when_not_installed(monkeypatch):
    # Make sure no google.generativeai is in sys.modules (it isn't by
    # default in this test env, but be explicit).
    monkeypatch.delitem(sys.modules, "google.generativeai", raising=False)
    monkeypatch.delitem(sys.modules, "google", raising=False)
    from lightsei.integrations.gemini_patch import patch_gemini
    # The import inside patch_gemini will raise ImportError; the helper
    # should swallow + return False rather than propagate.
    assert patch_gemini() is False


def test_patch_gemini_returns_true_when_installed(monkeypatch):
    _install_fake_gemini(monkeypatch)
    from lightsei.integrations.gemini_patch import patch_gemini
    assert patch_gemini() is True


# ---------- emitted events ---------- #


def test_generate_content_emits_started_and_completed(monkeypatch):
    _install_fake_gemini(monkeypatch)
    from lightsei.integrations.gemini_patch import patch_gemini

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(_client, "emit", lambda kind, payload, **kw: emitted.append((kind, payload)))
    monkeypatch.setattr(
        _client, "check_policy", lambda action, payload=None, **kw: {"allow": True}
    )

    patch_gemini()
    from google.generativeai import GenerativeModel  # type: ignore

    model = GenerativeModel("gemini-1.5-flash")
    resp = model.generate_content("hello")
    assert resp.text == "hello from gemini"

    kinds = [k for k, _ in emitted]
    assert "llm_call_started" in kinds
    assert "llm_call_completed" in kinds

    completed = next(p for k, p in emitted if k == "llm_call_completed")
    assert completed["model"] == "gemini-1.5-flash"
    assert completed["input_tokens"] == 12
    assert completed["output_tokens"] == 7
    assert completed["total_tokens"] == 19
    assert "duration_s" in completed


def test_generate_content_strips_models_prefix(monkeypatch):
    _install_fake_gemini(monkeypatch)
    from lightsei.integrations.gemini_patch import patch_gemini

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(_client, "emit", lambda kind, payload, **kw: emitted.append((kind, payload)))
    monkeypatch.setattr(
        _client, "check_policy", lambda action, payload=None, **kw: {"allow": True}
    )

    patch_gemini()
    from google.generativeai import GenerativeModel  # type: ignore

    model = GenerativeModel("models/gemini-1.5-pro")
    model.generate_content("hello")

    completed = next(p for k, p in emitted if k == "llm_call_completed")
    # The "models/" prefix is stripped so the recorded model lines up
    # with PRICING keys (gemini-1.5-pro), not "models/gemini-1.5-pro".
    assert completed["model"] == "gemini-1.5-pro"


def test_patch_is_idempotent(monkeypatch):
    fake_module, fake_cls = _install_fake_gemini(monkeypatch)
    from lightsei.integrations.gemini_patch import patch_gemini

    patch_gemini()
    first_wrapped = fake_cls.generate_content
    patch_gemini()
    second_wrapped = fake_cls.generate_content
    # Re-patching should be a no-op — the marker on the class blocks
    # double-wrapping (otherwise emit would fire twice per call).
    assert first_wrapped is second_wrapped


def test_check_policy_deny_raises_and_emits_policy_denied(monkeypatch):
    _install_fake_gemini(monkeypatch)
    from lightsei.integrations.gemini_patch import patch_gemini
    from lightsei.errors import LightseiPolicyError

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(_client, "emit", lambda kind, payload, **kw: emitted.append((kind, payload)))
    monkeypatch.setattr(
        _client,
        "check_policy",
        lambda action, payload=None, **kw: {"allow": False, "reason": "test deny"},
    )

    patch_gemini()
    from google.generativeai import GenerativeModel  # type: ignore

    model = GenerativeModel("gemini-1.5-flash")
    with pytest.raises(LightseiPolicyError):
        model.generate_content("hello")

    assert any(k == "policy_denied" for k, _ in emitted)
    # Underlying call should NOT have run; therefore no completed/started.
    assert not any(k == "llm_call_completed" for k, _ in emitted)


def test_streaming_call_passes_through_uninstrumented(monkeypatch):
    """For 12.2 first slice we punt on Gemini stream taps — pass the
    stream object through unchanged. Verify the patched method doesn't
    crash and doesn't emit completed (since the first-slice patch only
    emits on the non-streaming path)."""
    _install_fake_gemini(monkeypatch)
    from lightsei.integrations.gemini_patch import patch_gemini

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(_client, "emit", lambda kind, payload, **kw: emitted.append((kind, payload)))
    monkeypatch.setattr(
        _client, "check_policy", lambda action, payload=None, **kw: {"allow": True}
    )

    patch_gemini()
    from google.generativeai import GenerativeModel  # type: ignore

    model = GenerativeModel("gemini-1.5-flash")
    resp = model.generate_content("hello", stream=True)
    # The fake doesn't return an actual stream object — it returns a
    # FakeResponse — but the patched code routes around the
    # instrumentation entirely on stream=True, which is what we're
    # asserting.
    assert resp is not None
    assert not any(k == "llm_call_completed" for k, _ in emitted)
