"""Phase 35.1: assistant identities (constellation names + roles).

The business personas have internal ids (`bi`, `inbox`, …) that the
feeders target and the worker deploys — those never change. This module is
the *customer-facing* layer: each assistant gets a distinctive star name
plus a plain-English role, so the team reads as a branded constellation
("Lyra · Reputation") rather than a generic feature list, while the role
label keeps "who does what" obvious.

Names are display-only and tunable. Phase 35.2 adds a per-workspace rename
override on top of these defaults.
"""
from __future__ import annotations

from typing import Any, Optional

# Internal agent id -> {name (star), role (plain English)}. Star names are
# chosen to avoid the internal dev-bot names (argus/atlas/vega/sirius/
# cassiopeia/polaris/hermes) so the two never collide on screen.
DEFAULT_IDENTITY: dict[str, dict[str, str]] = {
    "website": {"name": "Rigel", "role": "Website"},
    "lead": {"name": "Orion", "role": "Leads"},
    "reputation": {"name": "Lyra", "role": "Reputation"},
    "marketing": {"name": "Nova", "role": "Marketing"},
    "bi": {"name": "Altair", "role": "Business Intelligence"},
    "inbox": {"name": "Mira", "role": "Inbox"},
    "seo": {"name": "Spica", "role": "SEO"},
}


def identity(agent_name: str, override: Optional[str] = None) -> dict[str, Any]:
    """Resolve an assistant's display identity.

    Returns {agent, name, role, is_default}. `override` (a per-workspace
    rename, blank/None for none) wins over the star default for `name`; the
    role always comes from the default. An unknown agent falls back to a
    title-cased id with no role.
    """
    base = DEFAULT_IDENTITY.get(agent_name)
    default_name = base["name"] if base else agent_name.replace("_", " ").title()
    role = base["role"] if base else None

    chosen = (override or "").strip() or default_name
    return {
        "agent": agent_name,
        "name": chosen,
        "role": role,
        "is_default": not (override or "").strip(),
    }


def display_label(agent_name: str, override: Optional[str] = None) -> str:
    """A one-line label for compact surfaces: 'Lyra · Reputation' (or just
    the name when there's no role)."""
    ident = identity(agent_name, override)
    return f"{ident['name']} · {ident['role']}" if ident["role"] else ident["name"]
