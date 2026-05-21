"""Phase 16.7: trust-zone presets.

Three named presets the team-from-README flow picks one of (default
`'standard_team'`). Each preset maps a role to its starting
trust-zone configuration:

    role -> {sensitivity_level, capabilities, dispatches_cross_zone}

When the user deploys a team-from-README plan with preset P, the
dashboard applies `apply_preset(P, member.role)` to each generated
agent — sets sensitivity_level via PATCH /agents/{name}, capabilities
via PATCH /agents/{name}/capabilities, dispatches_cross_zone via
PATCH /agents/{name}. After that the bots' Phase 16.3 + 16.4 + 16.5
gates kick in automatically.

The three presets and what they imply for the buyer:

- **open_team**: developer-convenience. Everything's allowed
  (`'internet'` + `'send_command'` granted to every role), every
  agent in the `'public'` zone. Use when none of the agents touch
  customer data and the user wants minimum configuration friction.

- **standard_team**: SMB defaults. `'internal'` zone for every agent,
  `'send_command'` granted (so the constellation can dispatch
  internally), `'internet'` granted only to roles that typically
  need it (specialist + messenger), cross-zone dispatch disabled
  everywhere (no boundaries crossed). The middle of the road; what
  most non-pii workspaces want.

- **compliance_team**: the canonical CRM-bot scenario. Specialists
  default to `'pii'` (they're the ones touching customer data) with
  NO internet + NO send_command (so they can't exfiltrate even if
  prompt-injected). Messengers default to `'public'` with internet
  (they're the outbound side) and `'send_command'` enabled. Cross-
  zone dispatch DISABLED everywhere — the only way data crosses
  zones is via the human-mediated `lightsei.handoff_span` from
  Phase 16.5. This is the proof point against Viktor: their model
  can't isolate this; ours does by default.

Preset names are stable identifiers; renaming one would require a
dashboard migration. The dashboard's preset picker pulls the
displayable metadata (label, description, tradeoff line) from
`PRESET_METADATA` so wording can iterate freely.
"""
from __future__ import annotations

from typing import Any, Optional


# Stable identifiers. Changing one renames the preset everywhere
# (dashboard, telemetry, future analytics on "what preset did
# people pick").
OPEN_TEAM = "open_team"
STANDARD_TEAM = "standard_team"
COMPLIANCE_TEAM = "compliance_team"

VALID_PRESETS: frozenset[str] = frozenset(
    {OPEN_TEAM, STANDARD_TEAM, COMPLIANCE_TEAM}
)

DEFAULT_PRESET = STANDARD_TEAM


# Per-role default config. team_planner.py uses the three-role
# vocabulary (orchestrator / specialist / messenger); the agents
# table has a richer vocabulary (also executor / notifier). We
# cover both by treating executor as specialist and notifier as
# messenger when we hit an unknown role.
_ROLE_ALIASES: dict[str, str] = {
    "executor": "specialist",
    "notifier": "messenger",
}


def _normalize_role(role: Optional[str]) -> str:
    """Map a role to one of the three trust-zone-meaningful roles.
    Unknown / missing → 'specialist' as a sensible middle ground."""
    if not isinstance(role, str):
        return "specialist"
    role = role.strip().lower()
    if role in {"orchestrator", "specialist", "messenger"}:
        return role
    return _ROLE_ALIASES.get(role, "specialist")


ZONE_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    OPEN_TEAM: {
        "orchestrator": {
            "sensitivity_level": "public",
            "capabilities": ["internet", "send_command"],
            "dispatches_cross_zone": True,
        },
        "specialist": {
            "sensitivity_level": "public",
            "capabilities": ["internet", "send_command"],
            "dispatches_cross_zone": True,
        },
        "messenger": {
            "sensitivity_level": "public",
            "capabilities": ["internet", "send_command"],
            "dispatches_cross_zone": True,
        },
    },
    STANDARD_TEAM: {
        # Orchestrator coordinates; needs send_command but doesn't
        # need to make outbound web calls itself (its specialists do).
        "orchestrator": {
            "sensitivity_level": "internal",
            "capabilities": ["send_command"],
            "dispatches_cross_zone": False,
        },
        # Specialists do the work — typically need both.
        "specialist": {
            "sensitivity_level": "internal",
            "capabilities": ["internet", "send_command"],
            "dispatches_cross_zone": False,
        },
        # Messengers are leaves; they send outbound notifications.
        # No send_command (a leaf shouldn't dispatch further).
        "messenger": {
            "sensitivity_level": "internal",
            "capabilities": ["internet"],
            "dispatches_cross_zone": False,
        },
    },
    COMPLIANCE_TEAM: {
        # Fallback by_role for planner outputs that lack sensitivity_hint
        # (older planner runs before P16.x). The hint-aware mapping below
        # is the preferred path; this is just here so deploys don't break
        # when the hint is missing.
        "orchestrator": {
            "sensitivity_level": "internal",
            "capabilities": ["send_command"],
            "dispatches_cross_zone": False,
        },
        "specialist": {
            "sensitivity_level": "pii",
            "capabilities": [],
            "dispatches_cross_zone": False,
        },
        "messenger": {
            "sensitivity_level": "public",
            "capabilities": ["internet"],
            "dispatches_cross_zone": False,
        },
    },
}


# Defense against a preset dict drifting out of sync with the role
# normalization. Caught at import time as a loud failure.
for _name, _by_role in ZONE_PRESETS.items():
    assert set(_by_role.keys()) == {"orchestrator", "specialist", "messenger"}, (
        f"preset {_name!r} missing required role keys: "
        f"got {sorted(_by_role.keys())}"
    )


# P16.x: hint-aware preset mappings. When the planner emits a
# `sensitivity_hint` per bot, the dashboard prefers this map over
# the role-based one. Today only the Compliance preset is hint-aware
# — Open and Standard don't gain anything from per-bot zones since
# they apply the same zone everywhere — but the data shape is uniform
# so the dashboard's preset code can treat all three identically.
#
# Why these defaults:
#  - pii: no capabilities. A pii bot can't make outbound network
#    calls (would defeat the boundary) and can't dispatch (cross-zone
#    is blocked; same-zone would also need send_command).
#  - sensitive: same locked-down posture as pii since "sensitive"
#    data still shouldn't leak via prompt injection.
#  - internal: send_command + internet. Internal bots coordinate
#    within the chain and may need to post to Slack / webhooks (which
#    require `internet`). Cross-zone still disabled.
#  - public: internet only. Public bots are leaves; they don't dispatch
#    back to internal/pii (one-way boundary). Cross-zone still disabled.
HINT_AWARE_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    OPEN_TEAM: {},  # not hint-aware; dashboard falls back to by_role
    STANDARD_TEAM: {},  # same
    COMPLIANCE_TEAM: {
        "pii": {
            "sensitivity_level": "pii",
            "capabilities": [],
            "dispatches_cross_zone": False,
        },
        "sensitive": {
            "sensitivity_level": "sensitive",
            "capabilities": [],
            "dispatches_cross_zone": False,
        },
        "internal": {
            "sensitivity_level": "internal",
            # Phase 19.5: slack:respond added so internal bots are
            # reachable from chat channels in their zone. The
            # orchestrator (19.4) filters routing candidates on this
            # capability + zone match.
            "capabilities": ["send_command", "internet", "slack:respond"],
            "dispatches_cross_zone": False,
        },
        "public": {
            "sensitivity_level": "public",
            "capabilities": ["internet", "slack:respond"],
            "dispatches_cross_zone": False,
        },
    },
}


VALID_SENSITIVITY_HINTS: frozenset[str] = frozenset(
    {"public", "internal", "sensitive", "pii"}
)


# Same import-time safety net for the hint-aware map.
for _name, _by_hint in HINT_AWARE_PRESETS.items():
    if _by_hint:
        assert set(_by_hint.keys()) == VALID_SENSITIVITY_HINTS, (
            f"preset {_name!r} hint mapping missing required keys: "
            f"got {sorted(_by_hint.keys())}"
        )


# Dashboard-facing metadata. Separated from ZONE_PRESETS so wording
# can iterate without touching the role configs the gates depend on.
PRESET_METADATA: dict[str, dict[str, str]] = {
    OPEN_TEAM: {
        "label": "Open team",
        "summary": "Developer convenience — internet + dispatch everywhere.",
        "tradeoff": (
            "Use when none of the agents touch customer data and you "
            "want minimum configuration friction. Every agent gets "
            "every capability; trust zones don't enforce anything."
        ),
    },
    STANDARD_TEAM: {
        "label": "Standard team",
        "summary": "SMB defaults — internal zone, no cross-zone dispatch.",
        "tradeoff": (
            "The middle of the road. Agents can talk to each other and "
            "to the internet, but stay in the internal zone. Pick this "
            "if you're not sure which preset fits."
        ),
    },
    COMPLIANCE_TEAM: {
        "label": "Compliance team",
        "summary": "Your customer data does not leave the team.",
        "tradeoff": (
            "PII-side specialists have NO internet and NO send_command. "
            "Public-side messengers have internet but no dispatch back "
            "to PII. The only way data crosses zones is via a "
            "human-mediated handoff via lightsei.handoff_span. Use for "
            "CRM / healthcare / any workflow where PII must not leak."
        ),
    },
}


# Same invariant for metadata.
assert set(PRESET_METADATA.keys()) == VALID_PRESETS, (
    f"PRESET_METADATA keys {sorted(PRESET_METADATA.keys())} != "
    f"VALID_PRESETS {sorted(VALID_PRESETS)}"
)


def apply_preset(
    preset_name: str,
    role: Optional[str],
    sensitivity_hint: Optional[str] = None,
) -> dict[str, Any]:
    """Return the trust-zone config for one bot under one preset.

    If `sensitivity_hint` is provided AND the preset has a hint-aware
    mapping for that hint, the hint wins — this is the P16.x path where
    the planner labels each bot's intent and the preset uses it rather
    than falling back to role-based defaults.

    If the hint is missing OR the preset isn't hint-aware (Open / Standard),
    falls back to role-based mapping. Unknown preset → DEFAULT_PRESET.
    Unknown role → 'specialist' via `_normalize_role`. Unknown hint →
    falls back to role.

    Returns a deep-copied dict so the caller can mutate the returned
    config without poisoning the next call.
    """
    if preset_name not in VALID_PRESETS:
        preset_name = DEFAULT_PRESET

    # Hint-aware path: planner emitted a sensitivity_hint AND this preset
    # has a mapping for it.
    if (
        sensitivity_hint is not None
        and sensitivity_hint in VALID_SENSITIVITY_HINTS
        and HINT_AWARE_PRESETS.get(preset_name)
    ):
        src = HINT_AWARE_PRESETS[preset_name][sensitivity_hint]
        return {
            "sensitivity_level": src["sensitivity_level"],
            "capabilities": list(src["capabilities"]),
            "dispatches_cross_zone": src["dispatches_cross_zone"],
        }

    # Role-based fallback (legacy path + non-hint-aware presets).
    normalized = _normalize_role(role)
    src = ZONE_PRESETS[preset_name][normalized]
    return {
        "sensitivity_level": src["sensitivity_level"],
        "capabilities": list(src["capabilities"]),
        "dispatches_cross_zone": src["dispatches_cross_zone"],
    }


def list_presets() -> list[dict[str, Any]]:
    """Returns the presets in a dashboard-renderable shape:
    `[{name, label, summary, tradeoff, by_role, by_hint, is_default}]`.
    `by_hint` is an empty dict for non-hint-aware presets (Open, Standard);
    the dashboard treats an empty dict as "no hint-aware mapping, fall back
    to by_role".

    Stable ordering: open, standard, compliance — left-to-right least-to-most
    restrictive so the picker reads as a slider."""
    ordered = [OPEN_TEAM, STANDARD_TEAM, COMPLIANCE_TEAM]
    return [
        {
            "name": name,
            "label": PRESET_METADATA[name]["label"],
            "summary": PRESET_METADATA[name]["summary"],
            "tradeoff": PRESET_METADATA[name]["tradeoff"],
            "by_role": {
                role: dict(cfg) for role, cfg in ZONE_PRESETS[name].items()
            },
            "by_hint": {
                hint: dict(cfg)
                for hint, cfg in HINT_AWARE_PRESETS.get(name, {}).items()
            },
            "is_default": name == DEFAULT_PRESET,
        }
        for name in ordered
    ]
