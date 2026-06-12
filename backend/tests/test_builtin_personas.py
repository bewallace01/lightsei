"""Phase 33.2: built-in persona bundle tests.

The drift guard is the important one: the vendored copies under
backend/builtin_personas/ must stay byte-identical to the source of truth
in the repo's top-level agents/. If someone edits a persona in agents/ and
forgets to re-vendor, this fails loudly.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest

import builtin_personas

_HERE = os.path.dirname(__file__)
_AGENTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "agents"))


@pytest.mark.parametrize("name", builtin_personas.BUILTIN_PERSONAS)
@pytest.mark.parametrize("fname", ["bot.py", "requirements.txt"])
def test_vendored_copy_matches_agents_source(name, fname):
    """Drift guard: backend/builtin_personas/<name> == agents/<name>."""
    src = os.path.join(_AGENTS_DIR, name, fname)
    vendored = os.path.join(builtin_personas.persona_dir(name), fname)
    if not os.path.exists(src):
        pytest.skip(f"agents/{name}/{fname} not present in this checkout")
    with open(src, "rb") as a, open(vendored, "rb") as b:
        assert a.read() == b.read(), (
            f"backend/builtin_personas/{name}/{fname} has drifted from "
            f"agents/{name}/{fname}; re-vendor it"
        )


def test_build_bundle_has_bot_and_requirements_at_root():
    data = builtin_personas.build_bundle_zip("bi")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    # The worker expects bot.py at the root (no wrapping directory).
    assert "bot.py" in names
    assert "requirements.txt" in names
    assert not any("/" in n for n in names)


def test_bundle_is_deterministic():
    # Same source -> same bytes -> same sha (fixed timestamps in the zip).
    assert builtin_personas.bundle_sha256("bi") == builtin_personas.bundle_sha256("bi")
    assert builtin_personas.build_bundle_zip("inbox") == builtin_personas.build_bundle_zip("inbox")


def test_unknown_persona_rejected():
    with pytest.raises(ValueError):
        builtin_personas.build_bundle_zip("not_a_persona")


def test_llm_personas_are_a_subset_of_roster():
    assert builtin_personas.LLM_PERSONAS <= set(builtin_personas.BUILTIN_PERSONAS)
