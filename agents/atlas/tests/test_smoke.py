"""Smoke test bundled with the atlas bot so a fresh deploy has
something pytest can actually collect. Replace this with the user's
real test suite once Atlas points at a checked-out repo (Phase 13+).
"""


def test_smoke():
    assert 1 + 1 == 2


def test_phase_11_7_demo_failure_path():
    # Phase 11.7 demo: this test fails on purpose so the chain produces a
    # ❌ Slack message via hermes. Remove or invert the assertion to
    # turn the chain green again.
    assert 0 == 1, "deliberate failure for the 11.7 failure-path demo"


def test_atlas_bundle_intact():
    # If pytest can import the bundle's bot.py, the bundle landed
    # cleanly on the worker.
    import importlib.util
    from pathlib import Path

    bot = Path(__file__).parent.parent / "bot.py"
    assert bot.exists()
    spec = importlib.util.spec_from_file_location("atlas_bot_smoke", str(bot))
    assert spec is not None
