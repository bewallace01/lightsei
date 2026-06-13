"""Phase 35.1: assistant identity (constellation names + roles)."""
from __future__ import annotations

import assistant_identity as ai


def test_default_identity_has_star_name_and_role():
    rep = ai.identity("reputation")
    assert rep["name"] == "Lyra"
    assert rep["role"] == "Reputation"
    assert rep["is_default"] is True


def test_every_persona_has_a_distinct_star_name():
    names = [ai.identity(a)["name"] for a in ai.DEFAULT_IDENTITY]
    assert len(names) == len(set(names))  # no two share a name


def test_override_replaces_name_keeps_role():
    out = ai.identity("bi", "Numbers")
    assert out["name"] == "Numbers"
    assert out["role"] == "Business Intelligence"  # role is fixed
    assert out["is_default"] is False


def test_blank_override_falls_back_to_default():
    assert ai.identity("inbox", "   ")["name"] == "Mira"
    assert ai.identity("inbox", None)["name"] == "Mira"


def test_unknown_agent_titlecases_with_no_role():
    out = ai.identity("some_new_bot")
    assert out["name"] == "Some New Bot"
    assert out["role"] is None


def test_display_label_pairs_name_and_role():
    assert ai.display_label("marketing") == "Nova · Marketing"
    assert ai.display_label("marketing", "Buzz") == "Buzz · Marketing"
    # No role -> just the name.
    assert ai.display_label("some_new_bot") == "Some New Bot"
