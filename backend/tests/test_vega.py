"""Phase 13.2: Vega PR-reviewer bot.

Drives `agents/vega/bot.py`'s diff review + tick in isolation with a
stubbed `lightsei` module, mirroring test_atlas.py / test_argus.py.

Coverage:
  - review_diff parses unified-diff added lines + flags eval/exec,
    swallowed exceptions, skipped tests, debug statements, leftover
    TODOs, source-without-tests, and oversized diffs, with correct
    file + line attribution.
  - clean diff -> no comments.
  - hermes_text_for summary formatting.
  - tick(): comments -> emit + dispatch hermes + complete.
  - tick(): clean diff -> emit, no dispatch, complete.
  - tick(): empty queue / unknown kind / crash paths.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


VEGA_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "vega" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)

    monkeypatch.delitem(sys.modules, "vega_bot", raising=False)
    spec = importlib.util.spec_from_file_location("vega_bot", str(VEGA_BOT_PATH))
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["vega_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _diff(file: str, added: list[str], start: int = 1, test_file: str | None = None) -> str:
    """Build a minimal unified diff that adds `added` lines to `file`
    starting at new-file line `start`. Optionally also touch a test file
    so the source-without-tests heuristic can be exercised."""
    body = (
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n+++ b/{file}\n"
        f"@@ -0,0 +{start},{len(added)} @@\n"
        + "".join(f"+{ln}\n" for ln in added)
    )
    if test_file:
        body += (
            f"diff --git a/{test_file} b/{test_file}\n"
            f"--- a/{test_file}\n+++ b/{test_file}\n"
            f"@@ -0,0 +1,1 @@\n+def test_x(): pass\n"
        )
    return body


# ---------- review_diff ---------- #


def test_review_flags_eval_high_with_line_and_file(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("app/run.py", ["import os", "x = eval(user_input)"], start=10, test_file="tests/test_run.py")
    comments = bot.review_diff(patch)
    evals = [c for c in comments if c["message"] == "use of eval/exec"]
    assert len(evals) == 1
    assert evals[0]["severity"] == "high"
    assert evals[0]["file"] == "app/run.py"
    assert evals[0]["line"] == 11  # second added line, starting at 10


def test_review_flags_debug_and_todo(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("a.py", ["print('hi')", "# TODO clean this up"], test_file="tests/test_a.py")
    msgs = {c["message"] for c in bot.review_diff(patch)}
    assert "debug statement left in" in msgs
    assert "leftover TODO/FIXME" in msgs


def test_review_flags_swallowed_exception_and_skipped_test(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("svc.py", ["try:", "    do()", "except Exception: pass", "@pytest.mark.skip"], test_file="tests/test_svc.py")
    sev = {c["message"]: c["severity"] for c in bot.review_diff(patch)}
    assert sev.get("bare or swallowed exception") == "high"
    assert sev.get("skipped or focused test") == "high"


def test_review_source_without_tests_is_low(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("app/x.py", ["x = 1"])  # no test file touched
    comments = bot.review_diff(patch)
    assert any(c["message"] == "source changed but no test changes in this diff"
               and c["severity"] == "low" for c in comments)


def test_review_source_with_tests_has_no_missing_test_note(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("app/x.py", ["x = 1"], test_file="tests/test_x.py")
    assert not any("no test changes" in c["message"] for c in bot.review_diff(patch))


def test_review_large_diff_note(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("app/x.py", [f"a{i} = {i}" for i in range(10)], test_file="tests/test_x.py")
    comments = bot.review_diff(patch, large_diff=5)
    assert any("large diff" in c["message"] for c in comments)


def test_review_clean_diff_no_comments(fake_lightsei):
    _, bot = fake_lightsei
    patch = _diff("app/x.py", ["def add(a, b):", "    return a + b"], test_file="tests/test_x.py")
    assert bot.review_diff(patch) == []
    assert bot.review_diff("") == []


# ---------- hermes_text_for ---------- #


def test_hermes_text_summary(fake_lightsei):
    _, bot = fake_lightsei
    comments = [
        {"severity": "high", "file": "a.py", "line": 1, "message": "use of eval/exec"},
        {"severity": "low", "file": None, "line": None, "message": "..."},
    ]
    text = bot.hermes_text_for(comments, 42, commit="ede6e01572f7abd6089c34")
    assert "vega:" in text
    assert "42 added lines" in text
    assert "2 comments" in text
    assert "1 high" in text
    assert "ede6e01" in text


# ---------- tick ---------- #


def _make_command(*, cmd_id="cmd-1", kind="vega.review", payload=None):
    return {"id": cmd_id, "agent_name": "vega", "kind": kind, "payload": payload or {}}


def test_tick_with_comments_emits_and_dispatches(fake_lightsei):
    fake, bot = fake_lightsei
    patch = _diff("app/run.py", ["x = eval(z)"])
    fake.claim_command.return_value = _make_command(payload={"diff": patch, "commit": "abc1234567"})
    result = bot.tick(fake, hermes_channel="reviews")
    assert result is not None
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "vega.review_complete"
    payload = fake.emit.call_args.args[1]
    assert payload["high_severity_count"] == 1
    assert payload["severity"] == "error"
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and kind == "hermes.post"
    assert hp["channel"] == "reviews"
    assert fake.send_command.call_args.kwargs["source_agent"] == "vega"
    fake.complete_command.assert_called_once()
    assert "result" in fake.complete_command.call_args.kwargs


def test_tick_clean_diff_no_dispatch(fake_lightsei):
    fake, bot = fake_lightsei
    patch = _diff("app/x.py", ["return 1"], test_file="tests/test_x.py")
    fake.claim_command.return_value = _make_command(payload={"diff": patch})
    bot.tick(fake)
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[1]["comments_count"] == 0
    fake.send_command.assert_not_called()
    fake.complete_command.assert_called_once()


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(kind="vega.dance")
    bot.tick(fake)
    fake.emit.assert_not_called()
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_crash_emits_vega_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _make_command(payload={"diff": "whatever"})
    monkeypatch.setattr(bot, "review_diff", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake)
    assert fake.emit.call_args.args[0] == "vega.crash"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs
