"""Phase 20.1: Lightsei connector registry.

The CONNECTOR_REGISTRY dict is the source of truth for what connectors
Lightsei knows about. Each entry is a ConnectorSpec describing:

- `display_label`: shown in the dashboard's /integrations cards.
- `oauth_provider`: which OAuth helper module handles auth ('google',
  'slack', 'stripe', ...). Lets us share OAuth code across connectors
  that use the same provider.
- `default_scopes`: the OAuth scopes Lightsei requests at install time.
  Granted scopes are stored per-installation in
  `connector_installations.scopes` (a subset — users can decline).
- `declared_zones`: which Phase 16 sensitivity zones a bot must live
  in to call this connector. Calls from out-of-zone bots are refused
  by the Phase 20.6 bot-callable endpoint.
- `manifest`: an MCP-flavored list of tool definitions the connector
  exposes. Each tool has a `name`, `description`, and JSON-schema
  `input_schema`. The 20.3-20.5 sub-tasks fill these out per connector.
- `invoke`: function `(tool_name, payload, access_token) -> dict`. The
  bot-callable endpoint dispatches to this; per-connector modules will
  swap in real implementations in 20.3-20.5. For 20.1 these are stubs
  that raise NotImplementedError.

Why a Python registry instead of a DB table: the connector set is
hardcoded for v1 — adding a connector means shipping new Python code
(the implementation). A DB table would add deploy-time churn without
buying us anything. Custom MCP-server support (the long-tail story
mentioned in the Phase 20 rough-shape) lands as a later sub-task and
will need a DB table; by then the v1 surface will be stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from models import _VALID_SENSITIVITY_LEVELS


# MCP-flavored tool definition. Compatible with Anthropic's
# tools=[...] tool-use shape so we can pass these directly to a model
# when the bot is built on Claude. Keep this minimal — MCP itself has
# more fields but `name`+`description`+`input_schema` is what call
# sites actually consume.
ConnectorToolManifest = dict[str, Any]


class ConnectorNotImplementedError(NotImplementedError):
    """Raised when a connector module's invoke() is called before the
    sub-task that implements it has shipped. The 20.6 endpoint catches
    this and returns 501 so the dashboard can surface 'this connector
    isn't ready yet' rather than a stack trace."""


@dataclass(frozen=True)
class ConnectorSpec:
    """Metadata + dispatch surface for one connector.

    Frozen so a misbehaving caller can't mutate the registry at runtime.
    Functions are referenced lazily (callables) so adding a new
    connector module doesn't require eagerly importing it; the
    bot-callable endpoint resolves the function at call time.
    """

    name: str
    display_label: str
    oauth_provider: str
    default_scopes: tuple[str, ...]
    declared_zones: frozenset[str]
    summary: str
    manifest: Callable[[], list[ConnectorToolManifest]]
    invoke: Callable[..., Any]

    def __post_init__(self) -> None:
        # Defensive: declared_zones must be a subset of the real
        # sensitivity ladder. A typo would silently lock the connector
        # out of every bot's zone (no match), which is hard to debug.
        bad = set(self.declared_zones) - _VALID_SENSITIVITY_LEVELS
        if bad:
            raise ValueError(
                f"connector {self.name!r} declared_zones has invalid "
                f"levels: {sorted(bad)}"
            )


def _not_implemented_invoke(connector_name: str) -> Callable[..., Any]:
    """Factory: returns an invoke() that raises with the connector name
    in the message. Used as a placeholder for 20.3-20.5."""
    def _invoke(*, tool_name: str, payload: dict, access_token: str) -> dict:
        raise ConnectorNotImplementedError(
            f"connector {connector_name!r} tool {tool_name!r} is not "
            f"implemented yet (lands in Phase 20.3-20.5)"
        )
    return _invoke


def _empty_manifest() -> list[ConnectorToolManifest]:
    """Placeholder manifest for 20.1. Each connector replaces this in
    its dedicated sub-task (20.3-20.5)."""
    return []


# ---------- v1 connector set: Gmail + Google Calendar + Google Drive ---------- #


CONNECTOR_REGISTRY: dict[str, ConnectorSpec] = {
    "gmail": ConnectorSpec(
        name="gmail",
        display_label="Gmail",
        oauth_provider="google",
        default_scopes=(
            "https://www.googleapis.com/auth/gmail.modify",
            "openid",
            "email",
        ),
        # Work email touches employee + customer correspondence —
        # internal at minimum, often sensitive (HR threads) or pii
        # (customer reply chains). Excluded from public: a public-zoned
        # research bot has no business in the email inbox.
        declared_zones=frozenset({"internal", "sensitive", "pii"}),
        summary=(
            "Send and search email on the connected account. Use for "
            "internal notifications, customer-reply automation, or "
            "digest-from-inbox bots."
        ),
        manifest=_empty_manifest,
        invoke=_not_implemented_invoke("gmail"),
    ),
    "google_calendar": ConnectorSpec(
        name="google_calendar",
        display_label="Google Calendar",
        oauth_provider="google",
        default_scopes=(
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.readonly",
            "openid",
            "email",
        ),
        # Scheduling spans every zone: public researchers schedule
        # external meetings; internal bots run team standups; pii
        # bots schedule customer check-ins. All four levels allowed.
        declared_zones=frozenset({"public", "internal", "sensitive", "pii"}),
        summary=(
            "Read and write calendar events on the connected account. "
            "Use for scheduling, conflict detection, or weekly-meeting "
            "digest bots."
        ),
        manifest=_empty_manifest,
        invoke=_not_implemented_invoke("google_calendar"),
    ),
    "google_drive": ConnectorSpec(
        name="google_drive",
        display_label="Google Drive",
        oauth_provider="google",
        default_scopes=(
            "https://www.googleapis.com/auth/drive",
            "openid",
            "email",
        ),
        # Workspace docs are internal at minimum (no reason for a
        # public-zoned research bot to touch internal docs). Sensitive
        # / pii bots can touch their respective folders within the
        # same OAuth grant.
        declared_zones=frozenset({"internal", "sensitive", "pii"}),
        summary=(
            "Read, write, and search files in the connected Google "
            "Drive. Use for document-fetch, snapshot-generation, or "
            "knowledge-base bots."
        ),
        manifest=_empty_manifest,
        invoke=_not_implemented_invoke("google_drive"),
    ),
}


def get_connector(connector_type: str) -> Optional[ConnectorSpec]:
    """Look up a connector by type name. Returns None for unknown
    types — callers (the bot-callable endpoint, the dashboard browse
    surface) handle that as 404."""
    return CONNECTOR_REGISTRY.get(connector_type)


def list_connectors() -> list[ConnectorSpec]:
    """All known connectors, in registry-declaration order. Stable
    ordering so the dashboard's card grid doesn't shuffle on every
    page load."""
    return list(CONNECTOR_REGISTRY.values())
