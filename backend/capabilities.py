"""Phase 16.2: trust-zone capability model.

Pure module. Owns the capability vocabulary, the validation function
the PATCH endpoint calls, and the per-sensitivity-level preset map
that the team-from-README integration in 16.7 will consume.

A capability is a short string the SDK gates outbound operations on
(Phase 16.3): `'internet'` (outbound HTTP), `'send_command'` (Phase
11 dispatch), `'connector:<name>'` (Phase 20 connectors). The
enforced set in 16.3 is just `{'internet', 'send_command'}`;
`connector:*` is accepted by the validator so users can future-proof
their configuration but isn't actually enforced until the connector
SDK lands.

Switch trigger (from TASKS.md Phase 16 intro): if the vocabulary
grows past ~30 distinct capabilities, namespace it properly or build
a real taxonomy. Until then this dict-and-prefix approach is fine.
"""
from __future__ import annotations

from typing import Any

from models import (
    DEFAULT_SENSITIVITY_LEVEL,
    _VALID_SENSITIVITY_LEVELS,
    is_valid_sensitivity_level,
)


# Concrete capabilities enforced or validated explicitly. Each one is
# either:
#   - enforced by 16.3 (the SDK refuses ops requiring it when not in
#     the agent's allow-list)
#   - reserved for future enforcement (16.4 ties send_command to
#     cross-zone checks; 'connector:*' lands when Phase 20 ships the
#     connector SDK)
KNOWN_CAPABILITIES: frozenset[str] = frozenset({
    "internet",
    "send_command",
    # Phase 19.5: required for lightsei.post_slack(). Granted by the
    # Compliance preset's `internal` and `public` hint mappings (those
    # bots are chat-reachable). pii + sensitive don't get it by default
    # — a PII bot literally cannot post to Slack until an operator
    # explicitly adds the capability.
    "slack:respond",
})

# Structural prefix accepted by the validator without requiring each
# specific connector be enumerated up front. A workspace can set
# `connector:hubspot` today and have it become enforceable the moment
# Phase 20 wires the connector SDK; until then it's a forward-compat
# placeholder.
_CAPABILITY_PREFIXES: tuple[str, ...] = ("connector:",)

# How long a capability string can reasonably be. Keep tight so a
# misconfigured caller can't write a multi-megabyte JSON value into
# the agent row.
_MAX_CAPABILITY_LEN = 64
_MAX_CAPABILITIES_PER_AGENT = 50


def is_valid_capability(name: object) -> bool:
    """One-shot check for a single capability string. None / non-str /
    too-long / unknown-with-no-prefix all return False."""
    if not isinstance(name, str):
        return False
    if not name:
        return False
    if len(name) > _MAX_CAPABILITY_LEN:
        return False
    if name in KNOWN_CAPABILITIES:
        return True
    # Prefix-match path: `connector:hubspot` is fine even though
    # `hubspot` isn't on the explicit list yet.
    for prefix in _CAPABILITY_PREFIXES:
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        # Reject empty or whitespace-only suffix (`connector:` alone
        # is meaningless; `connector: ` shouldn't slip through).
        if suffix and suffix.strip() == suffix and suffix.replace("-", "").replace("_", "").isalnum():
            return True
    return False


def validate_capability_list(names: object) -> list[str]:
    """Return a list of problems with the proposed capability list.
    Empty list = valid. Same shape as the team-planner / agent-generator
    validators so the PATCH endpoint can render `problems` directly.

    Checks:
      - Input is a list (not None, dict, scalar).
      - Total count is within the per-agent cap.
      - Each element is a valid capability (known or `connector:<name>`).
      - No duplicates.
    """
    problems: list[str] = []
    if not isinstance(names, list):
        problems.append(
            f"capabilities must be a list of strings (got {type(names).__name__})"
        )
        return problems
    if len(names) > _MAX_CAPABILITIES_PER_AGENT:
        problems.append(
            f"capabilities[] is {len(names)} long; cap is "
            f"{_MAX_CAPABILITIES_PER_AGENT}"
        )
    seen: set[str] = set()
    for i, name in enumerate(names):
        if not is_valid_capability(name):
            problems.append(
                f"capabilities[{i}] is not a valid capability "
                f"(got {name!r}; expected one of "
                f"{sorted(KNOWN_CAPABILITIES)} or 'connector:<name>')"
            )
            continue
        if name in seen:
            problems.append(f"capabilities[{i}] is a duplicate of an earlier entry")
            continue
        seen.add(name)
    return problems


def normalize_capability_list(names: list[str]) -> list[str]:
    """Dedup while preserving first-seen order. Called from the PATCH
    endpoint after validation passes so persisted lists stay tidy."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# ---------- Per-sensitivity-level presets ---------- #


# What capabilities a new agent gets by default at each sensitivity
# rung. Used by 16.7's team-from-README preset wiring + the manual
# `/zones` editor when the user picks a level without overriding the
# capability list.
#
# The principle: more sensitive = fewer defaults. Compliance bots
# (`'pii'`) start with literally nothing — every capability must be an
# explicit user choice. Public bots get the full open-research set.
_PRESETS_BY_LEVEL: dict[str, list[str]] = {
    "public": ["internet", "send_command"],
    "internal": ["send_command"],
    "sensitive": [],
    "pii": [],
}

# Defense against the `_PRESETS_BY_LEVEL` dict drifting out of sync with
# the `_VALID_SENSITIVITY_LEVELS` set in `models.py`. Caught at import
# time rather than as a runtime KeyError.
assert set(_PRESETS_BY_LEVEL.keys()) == set(_VALID_SENSITIVITY_LEVELS), (
    f"_PRESETS_BY_LEVEL keys {sorted(_PRESETS_BY_LEVEL.keys())} != "
    f"sensitivity levels {sorted(_VALID_SENSITIVITY_LEVELS)}"
)


def presets_for_level(level: str | None) -> list[str]:
    """Return the default capability list for `level`. Unknown levels
    (including None) fall back to the default sensitivity level's
    preset so callers can pass straight from a possibly-null DB value
    without an extra `is_valid_*` round-trip."""
    if not is_valid_sensitivity_level(level):
        level = DEFAULT_SENSITIVITY_LEVEL
    return list(_PRESETS_BY_LEVEL[level])
