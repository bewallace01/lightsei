"""Smoke test bundled with the atlas bot so a fresh deploy has
something pytest can actually collect. Replace this with the user's
real test suite once Atlas points at a checked-out repo (Phase 13+).
"""


def test_smoke():
    assert 1 + 1 == 2


def test_atlas_bundle_intact():
    # If pytest can import the bundle's bot.py, the bundle landed
    # cleanly on the worker.
    import importlib.util
    from pathlib import Path

    bot = Path(__file__).parent.parent / "bot.py"
    assert bot.exists()
    spec = importlib.util.spec_from_file_location("atlas_bot_smoke", str(bot))
    assert spec is not None
