"""Phase 33.2: built-in persona bundles.

Onboarding provisions the assistant *rows*; to make the team actually run,
the persona bot *code* has to reach the worker as a deployment bundle. The
worker deploys from a zip blob, and the backend's build context excludes
the repo's top-level `agents/` dir, so the persona code the backend can
deploy is vendored here under `builtin_personas/<name>/`.

These vendored copies are byte-identical to `agents/<name>/` and a drift
test (tests/test_builtin_personas.py) enforces that, so the source of
truth stays `agents/` and this is just the deployable mirror.

`build_bundle_zip` produces exactly what the worker expects: bot.py +
requirements.txt at the zip root (no wrapping directory).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# The business-persona roster onboarding can deploy. Names match the
# agent_name the feeders target + the worker runs.
BUILTIN_PERSONAS = [
    "website",
    "lead",
    "reputation",
    "marketing",
    "bi",
    "inbox",
]

# LLM-backed personas need ANTHROPIC_API_KEY in the workspace secrets to do
# real work; the heuristic ones (website, lead, reputation) don't. Used to
# tell an owner "add an API key" after deploying.
LLM_PERSONAS = {"marketing", "bi", "inbox"}

# Capabilities each bundled persona needs for the code it ships with.
# Connector capabilities stay goal-specific and are granted by onboarding.
_REQUIRED_CAPABILITIES = {
    "website": ["internet", "send_command"],
    "lead": ["send_command"],
    "reputation": ["send_command"],
    "marketing": ["internet", "send_command"],
    "bi": ["internet", "send_command"],
    "inbox": ["internet", "send_command"],
}

_BASE_DIR = os.path.join(os.path.dirname(__file__), "builtin_personas")
# Files copied into each bundle, in a stable order so the zip bytes (and
# thus the sha) are deterministic.
_BUNDLE_FILES = ["bot.py", "requirements.txt"]


def is_builtin_persona(name: str) -> bool:
    return name in BUILTIN_PERSONAS


def required_capabilities(name: str) -> list[str]:
    return list(_REQUIRED_CAPABILITIES.get(name, []))


def grant_required_capabilities(
    session: Session, workspace_id: str, name: str, now: datetime
) -> bool:
    """Add the capabilities a bundled persona needs, preserving extra grants."""
    required = required_capabilities(name)
    if not required:
        return False
    row = session.execute(
        text(
            "SELECT capabilities FROM agents "
            "WHERE workspace_id = :w AND name = :n"
        ),
        {"w": workspace_id, "n": name},
    ).first()
    if row is None:
        return False
    caps = list(row[0] or [])
    changed = False
    for cap in required:
        if cap not in caps:
            caps.append(cap)
            changed = True
    if not changed:
        return False
    session.execute(
        text(
            "UPDATE agents SET capabilities = CAST(:caps AS JSONB), "
            "updated_at = :now WHERE workspace_id = :w AND name = :n"
        ),
        {"caps": json.dumps(caps), "now": now, "w": workspace_id, "n": name},
    )
    return True


def persona_dir(name: str) -> str:
    if not is_builtin_persona(name):
        raise ValueError(f"unknown builtin persona {name!r}")
    return os.path.join(_BASE_DIR, name)


def build_bundle_zip(name: str) -> bytes:
    """Zip a persona's files with bot.py + requirements.txt at the root.

    Deterministic: fixed file order + a fixed timestamp, so the same source
    always produces the same bytes (and the same sha). The worker unzips
    this and runs bot.py from the root.
    """
    directory = persona_dir(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in _BUNDLE_FILES:
            path = os.path.join(directory, fname)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            # Fixed metadata (date_time + external_attr) so the archive is
            # byte-stable across builds, not stamped with "now".
            info = zipfile.ZipInfo(fname, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def bundle_sha256(name: str) -> str:
    return hashlib.sha256(build_bundle_zip(name)).hexdigest()
