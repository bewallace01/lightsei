"""Phase 11.5: Polaris's `polaris.evaluate_push` handler.

Drives `polaris/bot.py:evaluate_push` in isolation. Same import-by-path
trick as test_atlas.py because polaris/bot.py and agents/atlas/bot.py
are both named `bot`, so loading them both as the bare `bot` module
collides.

Coverage:
  - Default rules + a push touching `polaris/bot.py` dispatches one
    `atlas.run_tests` (the spec's headline test case).
  - A push touching only `*.md` matches no default rule → 0 dispatches.
  - A push touching paths outside any rule → 0 dispatches.
  - The dispatched command call carries source_agent='polaris' so 11.6's
    constellation edges + 11.2's per-day caps attribute correctly.
  - Custom POLARIS_PUSH_RULES env overrides the defaults.
  - Malformed rules in the env are silently dropped (don't crash).
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


POLARIS_BOT_PATH = (
    Path(__file__).parent.parent.parent / "polaris" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    """Stub the `lightsei` module with mockable callables and load
    polaris/bot.py from its file path under the unique module name
    `polaris_bot_under_test` so it doesn't collide with atlas's `bot`
    in test_atlas.py.
    """
    fake = types.ModuleType("lightsei")
    fake.send_command = MagicMock(return_value={"id": "atlas-cmd-1"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    fake.flush = MagicMock()
    # @lightsei.track and @lightsei.on_command must return the function
    # unchanged so the decorated targets remain callable in tests.
    fake.track = lambda fn: fn
    fake.on_command = lambda *a, **kw: (lambda fn: fn)
    monkeypatch.setitem(sys.modules, "lightsei", fake)

    monkeypatch.delitem(sys.modules, "polaris_bot_under_test", raising=False)
    spec = importlib.util.spec_from_file_location(
        "polaris_bot_under_test", str(POLARIS_BOT_PATH)
    )
    assert spec is not None and spec.loader is not None
    bot = importlib.util.module_from_spec(spec)
    sys.modules["polaris_bot_under_test"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


# ---------- _parse_push_rules ---------- #


def test_parse_push_rules_default_when_unset(fake_lightsei):
    _, bot = fake_lightsei
    rules = bot._parse_push_rules(None)
    assert rules == [
        ("backend/**", "atlas.run_tests"),
        ("polaris/**", "atlas.run_tests"),
    ]


def test_parse_push_rules_default_when_empty(fake_lightsei):
    _, bot = fake_lightsei
    assert bot._parse_push_rules("") == bot._parse_push_rules(None)
    assert bot._parse_push_rules("   ") == bot._parse_push_rules(None)


def test_parse_push_rules_strips_whitespace(fake_lightsei):
    _, bot = fake_lightsei
    rules = bot._parse_push_rules(
        "  backend/** : atlas.run_tests , polaris/** : atlas.run_tests  "
    )
    assert rules == [
        ("backend/**", "atlas.run_tests"),
        ("polaris/**", "atlas.run_tests"),
    ]


def test_parse_push_rules_drops_malformed_entries(fake_lightsei):
    _, bot = fake_lightsei
    rules = bot._parse_push_rules(
        "backend/**:atlas.run_tests,no_colon_entry,:empty_pattern,empty_kind:"
    )
    assert rules == [("backend/**", "atlas.run_tests")]


# ---------- evaluate_push: spec test cases ---------- #


def test_push_touching_polaris_bot_dispatches_one_atlas_run_tests(fake_lightsei):
    """Spec: push touching polaris/bot.py dispatches one atlas.run_tests."""
    fake, bot = fake_lightsei
    payload = {
        "commit_sha": "abc1234",
        "branch": "main",
        "touched_paths": ["polaris/bot.py"],
    }
    summary = bot.evaluate_push(payload)
    assert len(summary["dispatched"]) == 1
    d = summary["dispatched"][0]
    assert d["target_agent"] == "atlas"
    assert d["kind"] == "atlas.run_tests"
    fake.send_command.assert_called_once()
    target, kind, sent_payload = fake.send_command.call_args.args[:3]
    assert target == "atlas"
    assert kind == "atlas.run_tests"
    assert sent_payload["matched_pattern"] == "polaris/**"
    assert sent_payload["matched_paths"] == ["polaris/bot.py"]
    # source_agent flows through so 11.2 caps + 11.6 edges attribute correctly.
    assert fake.send_command.call_args.kwargs.get("source_agent") == "polaris"


def test_push_touching_only_md_dispatches_nothing(fake_lightsei):
    """Spec: push touching only *.md dispatches nothing.

    README.md / docs/foo.md don't match the default backend/** or
    polaris/** rules; the handler should fall through with 0 dispatches.
    """
    fake, bot = fake_lightsei
    payload = {
        "commit_sha": "x",
        "branch": "main",
        "touched_paths": ["README.md", "docs/intro.md"],
    }
    summary = bot.evaluate_push(payload)
    assert summary["dispatched"] == []
    assert summary["skipped_reason"] == "no rule matched any touched path"
    fake.send_command.assert_not_called()


def test_push_touching_paths_outside_any_rule_dispatches_nothing(fake_lightsei):
    """Spec: push touching paths outside any rule dispatches nothing."""
    fake, bot = fake_lightsei
    payload = {
        "commit_sha": "x",
        "branch": "main",
        "touched_paths": ["frontend/app.tsx", "config/something.yaml"],
    }
    summary = bot.evaluate_push(payload)
    assert summary["dispatched"] == []
    fake.send_command.assert_not_called()


def test_push_touching_backend_and_polaris_dispatches_one_per_rule(
    fake_lightsei,
):
    """Two default rules both match → two dispatches (one per rule).
    Atlas dedupes downstream via the per-day cap if needed; the handler
    itself emits one command per matching rule for clarity."""
    fake, bot = fake_lightsei
    payload = {
        "commit_sha": "x",
        "branch": "main",
        "touched_paths": ["backend/main.py", "polaris/bot.py"],
    }
    summary = bot.evaluate_push(payload)
    assert len(summary["dispatched"]) == 2
    patterns = [m["pattern"] for m in summary["matched_rules"]]
    assert sorted(patterns) == ["backend/**", "polaris/**"]
    assert fake.send_command.call_count == 2


def test_custom_rules_env_overrides_defaults(fake_lightsei):
    fake, bot = fake_lightsei
    payload = {
        "commit_sha": "x",
        "branch": "main",
        "touched_paths": ["sdk/lightsei/_commands.py"],
    }
    # Without override, no rule matches → 0 dispatches.
    assert bot.evaluate_push(payload)["dispatched"] == []

    # With a custom rule for sdk/**, we get one dispatch.
    summary = bot.evaluate_push(payload, rules_env="sdk/**:atlas.run_tests")
    assert len(summary["dispatched"]) == 1
    assert summary["matched_rules"][0]["pattern"] == "sdk/**"


def test_empty_touched_paths_skips(fake_lightsei):
    fake, bot = fake_lightsei
    summary = bot.evaluate_push(
        {"commit_sha": "x", "branch": "main", "touched_paths": []}
    )
    assert summary["dispatched"] == []
    assert summary["skipped_reason"] == "no touched paths in push payload"
    fake.send_command.assert_not_called()


def test_glob_pattern_is_directory_aware(fake_lightsei):
    """`backend/**` should NOT match `backendXYZ/foo.py` — same directory-
    boundary semantics as the existing _push_touched_path."""
    fake, bot = fake_lightsei
    payload = {
        "commit_sha": "x",
        "branch": "main",
        "touched_paths": ["backendXYZ/foo.py"],
    }
    summary = bot.evaluate_push(payload)
    assert summary["dispatched"] == []
