"""SQLAlchemy declarative models for Lightsei's Postgres schema."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


# Phase 16.1: trust-zone sensitivity ladder. Same vocabulary used on
# agents (the configuration knob) and runs (denormalized at run-create
# time so analytics over historical runs don't have to JOIN agents).
#
# Order is meaningful: public < internal < sensitive < pii. Phase 16.4's
# cross-zone dispatch check + 16.5's redaction defaults both look at
# the ladder position, not just the string equality.
_VALID_SENSITIVITY_LEVELS: frozenset[str] = frozenset(
    {"public", "internal", "sensitive", "pii"}
)
DEFAULT_SENSITIVITY_LEVEL = "internal"


def is_valid_sensitivity_level(level: object) -> bool:
    """Helper for endpoint + SDK input validation. None / non-str / off-list
    all return False; the caller decides whether to default or 4xx."""
    return isinstance(level, str) and level in _VALID_SENSITIVITY_LEVELS


# Phase 17.1: plan tiers + auth providers. Same dict-and-helper pattern
# as the sensitivity ladder so the API + UI layers can validate inputs
# against a single source of truth.
_VALID_PLAN_TIERS: frozenset[str] = frozenset({"free", "paid"})
DEFAULT_PLAN_TIER = "free"

_VALID_AUTH_PROVIDERS: frozenset[str] = frozenset(
    {"apikey", "magic_link", "google_oauth"}
)
DEFAULT_AUTH_PROVIDER = "apikey"


def is_valid_plan_tier(tier: object) -> bool:
    return isinstance(tier, str) and tier in _VALID_PLAN_TIERS


def is_valid_auth_provider(provider: object) -> bool:
    return isinstance(provider, str) and provider in _VALID_AUTH_PROVIDERS


# Phase 21.1: widget conversation state machine + message roles.
# Validation is app-side; the DB column is plain String (same pattern
# as plan_tier + sensitivity_level).
_VALID_WIDGET_CONVERSATION_STATUSES: frozenset[str] = frozenset(
    {"open", "escalated", "operator_owned", "resolved"}
)
DEFAULT_WIDGET_CONVERSATION_STATUS = "open"

_VALID_WIDGET_MESSAGE_ROLES: frozenset[str] = frozenset(
    {"user", "bot", "operator", "system"}
)


def is_valid_widget_conversation_status(status: object) -> bool:
    return (
        isinstance(status, str)
        and status in _VALID_WIDGET_CONVERSATION_STATUSES
    )


def is_valid_widget_message_role(role: object) -> bool:
    return isinstance(role, str) and role in _VALID_WIDGET_MESSAGE_ROLES


# Phase 22.1: trigger kinds. Same validate-app-side pattern as the
# widget statuses above. 'cron' uses a 5-field cron expression in
# `triggers.schedule`; 'webhook' uses `triggers.webhook_token_hash`
# for the public POST /triggers/{token}/fire path. Event-based
# kinds (Gmail label, Drive change) are parked to Phase 22B.
_VALID_TRIGGER_KINDS: frozenset[str] = frozenset({"cron", "webhook"})


def is_valid_trigger_kind(kind: object) -> bool:
    return isinstance(kind, str) and kind in _VALID_TRIGGER_KINDS


# Phase 23.1: workspace membership roles. Only 'owner' is inserted
# by v1 (one user per workspace creation). 'member' is reserved for
# Phase 23B's invite + accept flow. Same validate-app-side pattern
# as the kind / status vocabularies above.
_VALID_WORKSPACE_MEMBER_ROLES: frozenset[str] = frozenset({"owner", "member"})


def is_valid_workspace_member_role(role: object) -> bool:
    return isinstance(role, str) and role in _VALID_WORKSPACE_MEMBER_ROLES


# Phase 25.1: end-user auth providers. Magic link only in v1; Apple /
# Google OAuth deferred to 25B. Distinct from `_VALID_AUTH_PROVIDERS`
# (operator) because the two surfaces have different supported flows
# and the 'apikey' value never applies to end users.
_VALID_END_USER_AUTH_PROVIDERS: frozenset[str] = frozenset({"magic_link"})
DEFAULT_END_USER_AUTH_PROVIDER = "magic_link"


def is_valid_end_user_auth_provider(provider: object) -> bool:
    return (
        isinstance(provider, str)
        and provider in _VALID_END_USER_AUTH_PROVIDERS
    )


# Phase 25.1: how an end user got linked to a vendor (workspace). v1
# is invite-code only; 'direct_invite' (vendor types end-user email
# and Lightsei mails the link) and 'public_discovery' park to 27B.
_VALID_END_USER_VENDOR_LINK_VIA: frozenset[str] = frozenset(
    {"invite_code", "direct_invite", "public_discovery"}
)
DEFAULT_END_USER_VENDOR_LINK_VIA = "invite_code"


def is_valid_end_user_vendor_link_via(linked_via: object) -> bool:
    return (
        isinstance(linked_via, str)
        and linked_via in _VALID_END_USER_VENDOR_LINK_VIA
    )


# Phase 27.1: per-vendor end-user notification preference. Set via
# Phase 27.2's PATCH /me/end-user/vendors/{workspace_id} endpoint and
# read by Phase 28's push delivery before sending. 'mentions' is a
# future hook (bots will need to @-mention end users explicitly to
# trigger it); for v1 only 'all' and 'off' actually gate sends.
_VALID_NOTIFICATION_PREFS: frozenset[str] = frozenset(
    {"all", "mentions", "off"}
)
DEFAULT_NOTIFICATION_PREF = "all"


def is_valid_notification_pref(pref: object) -> bool:
    return isinstance(pref, str) and pref in _VALID_NOTIFICATION_PREFS


# Phase 26.1: vendor_slug format. Lives in user-facing URLs
# (`/c/{vendor_slug}`) so it has to be lowercase + URL-safe.
# 3-32 chars, [a-z0-9-], no leading or trailing dash. The
# leading/trailing-dash rule keeps URLs legible
# (`/c/-acme` looks broken) and prevents the empty-but-not-empty
# shape `/c/-`.
import re as _re

_VENDOR_SLUG_RE = _re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$")


def is_valid_vendor_slug(slug: object) -> bool:
    """3-32 chars, lowercase alphanumeric + dashes, no leading or
    trailing dash. Endpoint code uses this to validate the operator's
    proposed slug before hitting the unique constraint."""
    if not isinstance(slug, str):
        return False
    if len(slug) < 3 or len(slug) > 32:
        return False
    return _VENDOR_SLUG_RE.match(slug) is not None


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Phase 23.3: bumped on rename (PATCH /me/workspaces/{id}).
    # Nullable with server_default now() so the migration backfills
    # existing rows non-disruptively.
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        server_default=text("now()"),
    )
    # Phase 11B.1: workspace-level monthly spend cap. NULL = no cap.
    # When set and reached, runs in this workspace get denied with the same
    # UX path as Phase 2's per-agent daily cap.
    budget_usd_monthly: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    # Phase 17.1: Stripe customer-on-workspace-create. Null on workspaces
    # that predate billing or were created via the developer / API-key
    # signup path (those skip Stripe until they upgrade).
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, unique=True
    )
    # Set when the workspace has an active paid subscription. Cleared by
    # the Stripe webhook on cancel / payment_failed lifecycle events.
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    # 'free' (using credits) | 'paid' (active subscription). Single source
    # of truth for "should this workspace be allowed to spend right now."
    # See `_VALID_PLAN_TIERS` below + the paywall middleware in 17.5.
    plan_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="free"
    )
    # $5 of signup credits decrement on every Run row creation; both
    # lightsei.system (generation + judge) and bot-run cost come out of
    # the same pool. Paywall fires when this hits 0 and plan_tier='free'.
    free_credits_remaining_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="5.00",
        default=Decimal("5.00"),
    )
    # Phase 21.1: which bot answers widget chat messages for this
    # workspace. Operator picks via /widget-settings (21.7); the
    # 21.6 orchestrator reads it on every inbound message. Stored as
    # a plain string because agents use a composite PK
    # `(workspace_id, name)` so a real FK doesn't fit. App-side code
    # validates the name resolves to an Agent in this workspace at
    # both setting time and dispatch time.
    customer_facing_agent_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
    )
    # Phase 21.1: short URL-safe random string used in the widget
    # snippet's data-workspace attribute. Distinct from
    # `workspaces.id` so we can rotate it without breaking the
    # internal-id-stability promise. Unique across workspaces;
    # nullable because pre-21 rows don't have one yet.
    widget_public_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, unique=True,
    )
    # Phase 26.1: human-readable URL handle for the consumer chat
    # surface at /c/{vendor_slug}. Operator-claimed via
    # POST /workspaces/me/vendor-slug. Validated against
    # `is_valid_vendor_slug` (lowercase, 3-32, [a-z0-9-], no
    # leading/trailing dash). Unique across workspaces because the
    # slug appears in user-facing URLs and a collision would route
    # consumer traffic to the wrong vendor.
    vendor_slug: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, unique=True,
    )
    # Phase 21.1: HTTPS origins the widget POST endpoint will accept
    # requests from. Enforced against the `Origin` header in 21.2.
    # JSONB list of strings (e.g. ["https://app.halo.dev"]). Empty by
    # default; the widget settings page (21.7) lets the operator add
    # entries.
    allowed_widget_origins: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"),
    )
    # Phase 21.9: when the Polaris widget-incident-response scan
    # produces a suggested_fix on an escalation cluster, should we
    # automatically apply it (mutating the customer-facing bot's
    # system_prompt with the addendum) or wait for an operator to
    # click Apply in /inbox? Default false per CLAUDE.md's
    # "operator-driven" defaults. The dashboard exposes this on
    # /widget-settings; the scan reads it at apply time.
    polaris_auto_apply_widget_fixes: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_api_keys_workspace", "workspace_id"),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 11B.1: incrementally summed from `llm_call_completed` events
    # as they arrive at /events. Cached on the row so dashboard rollups
    # can aggregate cost in a single index scan instead of joining
    # events × pricing on every render. server_default '0' so existing
    # rows take a sane starting value before the migration's backfill
    # pass runs.
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    # Phase 16.1: snapshot of the agent's sensitivity_level at run-create
    # time. Denormalized so analytics queries (verdict rollups by zone,
    # cost rollups by zone, etc.) don't have to JOIN agents and lose
    # historical correctness if a user later changes an agent's level.
    # server_default 'internal' for the alembic backfill of existing rows.
    sensitivity_level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DEFAULT_SENSITIVITY_LEVEL,
    )
    # Phase 22.4: link back to the trigger that fired this run, if any.
    # SET NULL on trigger delete so the run row survives trigger cleanup.
    # NULL means "manual run" — the bot was kicked from the CLI, the
    # dashboard "run now" button, or a connector callback.
    triggered_by_trigger_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("triggers.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Phase 22.4: snapshot of triggers.kind at fire time. The FK above
    # goes NULL on trigger delete, but the /runs badge still wants to
    # render "Triggered by: cron" against historical rows. The snapshot
    # makes that possible without a JOIN that returns nothing.
    trigger_kind: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
    )

    __table_args__ = (
        Index("idx_runs_started_at", started_at.desc()),
        Index("idx_runs_ws_started_at", "workspace_id", started_at.desc()),
        # Phase 22.4: /runs?trigger_id= filter (22.8). Partial WHERE
        # keeps the index tight since most rows are manual.
        Index(
            "ix_runs_workspace_trigger",
            "workspace_id", "triggered_by_trigger_id",
            postgresql_where=text("triggered_by_trigger_id IS NOT NULL"),
        ),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("idx_events_run_id", "run_id"),
        Index("idx_events_agent_kind_ts", "agent_name", "kind", "timestamp"),
        Index(
            "idx_events_ws_agent_kind_ts",
            "workspace_id", "agent_name", "kind", "timestamp",
        ),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Phase 17.1: magic-link / OAuth verification status. False for users
    # created via the existing API-key signup path; True after the user
    # completes a magic-link round-trip OR signs in via Google OAuth with
    # a verified email.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False,
    )
    # Which signup path created this row. 'apikey' (existing flow),
    # 'magic_link' (Phase 17.2), or 'google_oauth' (Phase 17.3).
    auth_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DEFAULT_AUTH_PROVIDER,
        default=DEFAULT_AUTH_PROVIDER,
    )
    # Google's stable `sub` claim. Lets a returning OAuth user be matched
    # to the same User row even if they change their primary email at
    # Google. Unique-when-not-null; pre-OAuth users have NULL.
    google_user_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )

    __table_args__ = (
        Index("idx_users_workspace", "workspace_id"),
    )


class OAuthPendingState(Base):
    """Phase 17.3: short-lived state + PKCE store for the Google OAuth
    authorization-code flow.

    The /auth/google/start endpoint inserts a row before redirecting
    the user to Google; /auth/google/callback looks it up by state,
    pulls the code_verifier, exchanges the code for tokens, and deletes
    the row. 10-minute TTL covers the user's hop out to Google's
    consent screen + back. Left-behind rows from abandoned flows expire
    harmlessly — the next start always inserts a fresh row.
    """

    __tablename__ = "oauth_pending_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    # Where the dashboard wanted the user to land after signin. Lets
    # /auth/google/start be invoked from any signed-out page (login,
    # marketing site CTA, expired-session redirect) without losing the
    # original destination through the OAuth hop.
    redirect_after: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_oauth_pending_states_expires_at",
            "expires_at",
        ),
    )


class EmailSigninToken(Base):
    """Phase 17.1: single-use, 15-minute TTL token for the magic-link
    sign-in flow (Phase 17.2 consumes it).

    Stored as `token_hash` (sha256 hex of the plaintext) — same pattern
    as `api_keys.hash`. The plaintext goes in the magic-link URL; the
    server only ever sees the hash on consume. Database leak doesn't
    hand attackers active sign-in tokens.

    `consumed_at` makes the row a record rather than deleting it on
    consume — keeps an audit trail of "this token was used at T" for
    future security review.
    """

    __tablename__ = "email_signin_tokens"

    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Rate-limit query in 17.2 hits (email, created_at DESC) —
        # "how many tokens have we issued for this email in the last
        # hour?" Index leftmost on email so the same scan answers
        # "any active token for this email" probes too.
        Index(
            "ix_email_signin_tokens_email_created",
            "email", created_at.desc(),
        ),
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 23.1: which workspace this session is currently viewing.
    # 23.2 flips `get_workspace_id` to read from here on the
    # session-auth path. SET NULL on workspace delete so deleting a
    # workspace from another tab doesn't crash this session — the
    # next request 401s + redirects through the workspace picker
    # (23.6) instead. Nullable so the migration backfill is
    # painless; immediately populated for every existing session in
    # the same migration.
    active_workspace_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
        Index("ix_sessions_active_workspace", "active_workspace_id"),
    )


class WorkspaceMember(Base):
    """Phase 23.1: one row per (user, workspace) membership pair.

    Replaces the implicit "one workspace per user" relationship
    that lived on `users.workspace_id`. The legacy column stays
    in the schema for now — Phase 23.2 flips endpoint resolution
    to read from `sessions.active_workspace_id`, and a later
    cleanup drops `users.workspace_id` once nothing references
    it.

    v1 only inserts one row per workspace at create time
    (role='owner'); the composite-PK shape is built for Phase
    23B's invite + accept flow. Role values are validated app-
    side against `_VALID_WORKSPACE_MEMBER_ROLES`; not enforced
    at the DB so a future role addition doesn't need a migration.
    """

    __tablename__ = "workspace_members"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'owner'"),
        default="owner",
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        # Per-workspace roster (chronological). The PK already
        # covers the per-user lookup.
        Index(
            "ix_workspace_members_workspace",
            "workspace_id", "joined_at",
        ),
    )


class Agent(Base):
    __tablename__ = "agents"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String, primary_key=True)
    daily_cost_cap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    command_handlers: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 11B.3: drives the constellation map's tier rendering.
    # 'orchestrator' (Polaris) sits at the canvas center; 'executor'
    # in an inner ring at r=150; 'notifier' in an outer ring at r=250;
    # 'specialist' (future Argus / Vega) at r=200. Server_default
    # 'executor' so existing rows take a sensible value before any
    # explicit relabel.
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="executor"
    )
    # Phase 11.2: per-agent dispatch caps. Defaults are conservative
    # for the demo — bump via PATCH /agents/{name} once an agent has
    # earned trust. Phase 14's behavioral rules (layer 4) replace
    # these heuristics with proper streaming detection.
    max_dispatch_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    max_dispatch_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="100"
    )
    # Phase 12.1: per-agent LLM provider + model pin. Nullable; when null,
    # the constellation map + cost panel fall back to whatever the latest
    # `llm_call_completed` event reported. When set, a future scheduling
    # layer routes the agent's calls to the chosen provider deliberately.
    # `provider` is validated at the API layer against a small enum
    # {openai, anthropic, google, groq, xai, cohere}; not enforced at the
    # DB so a new adapter doesn't need a migration.
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Phase 12C-adjacent: cron-style bots (Polaris) read this at tick time
    # to override their POLL_S env default. Null = use the bot's own env
    # default. Reactive bots (atlas, hermes) ignore it.
    tick_interval_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Short "what this bot does" description, surfaced on the /agents
    # roster + the agent detail page. Auto-populated from the LLM
    # rationale when the bot is generated via 12B; hand-deployed bots
    # start null until the user writes one.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Phase 16.1: trust-zone sensitivity ladder. See
    # `_VALID_SENSITIVITY_LEVELS` at the top of this module. 16.3 (SDK
    # capability gate), 16.4 (cross-zone dispatch enforcement), and 16.5
    # (auto-redaction for `'pii'`) all read this column. server_default
    # 'internal' so the alembic backfill is a no-op for new installs
    # and the existing-row backfill in 0027 has a sensible target.
    sensitivity_level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DEFAULT_SENSITIVITY_LEVEL,
    )
    # Phase 16.2: per-agent capability allow-list. Default-deny — an
    # empty list means the SDK (Phase 16.3) refuses every gated op.
    # Vocabulary + validation live in `backend/capabilities.py`. JSONB
    # rather than String[] so we have room to grow each capability
    # into a dict (per-capability rate limits, etc.) without another
    # schema change.
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list,
    )
    # Phase 16.4: opt-in flag for cross-zone dispatch. Default False —
    # same-zone-only is the safer posture for a new agent. Setting
    # True allows this agent's send_command calls to target agents in
    # different sensitivity zones. Auto-approval rules from Phase 11.2
    # still apply on top — cross-zone-enabled does NOT mean
    # auto-approved.
    dispatches_cross_zone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_threads_ws_agent",
            "workspace_id", "agent_name", updated_at.desc(),
        ),
    )


class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="completed")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_thread_messages_thread", "thread_id", "created_at"),
    )


class TeamConversation(Base):
    __tablename__ = "team_conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_team_conversations_ws_updated",
            "workspace_id", updated_at.desc(),
        ),
    )


class TeamMessage(Base):
    __tablename__ = "team_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("team_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # "user" | "router" | "assistant"
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "pending" | "completed" | "error"
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="completed"
    )
    # Set on assistant rows; the deployed agent claims by name.
    # NULL on user + router rows.
    agent_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Set on router rows: {"agents": [{"name": str, "reason": str}, ...]}.
    # NULL on user + assistant rows.
    routed_agents: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_team_messages_conv_created",
            "conversation_id", "created_at",
        ),
        Index(
            "ix_team_messages_pending_assistant",
            "agent_name", "created_at",
            postgresql_where=text(
                "status = 'pending' AND role = 'assistant'"
            ),
        ),
    )


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    hostname: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sdk_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_agent_instances_ws_agent",
            "workspace_id", "agent_name", last_heartbeat_at.desc(),
        ),
    )


class DeploymentBlob(Base):
    __tablename__ = "deployment_blobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("idx_deployment_blobs_workspace", "workspace_id"),
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    desired_state: Mapped[str] = mapped_column(
        String, nullable=False, default="running"
    )
    source_blob_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("deployment_blobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Phase 10.3: provenance — 'cli' (SDK upload) or 'github_push'
    # (webhook-triggered). 'cli' is the default so existing rows
    # backfill correctly via migration 0017's server_default.
    source: Mapped[str] = mapped_column(String, nullable=False, default="cli")
    source_commit_sha: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_deployments_ws_agent",
            "workspace_id", "agent_name", created_at.desc(),
        ),
        Index("idx_deployments_claimable", "status", "desired_state"),
    )


class DeploymentLog(Base):
    __tablename__ = "deployment_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(
        String, ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stream: Mapped[str] = mapped_column(String, nullable=False)
    line: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_deployment_logs_dep", "deployment_id", "id"),
    )


class WorkspaceSecret(Base):
    __tablename__ = "workspace_secrets"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Command(Base):
    __tablename__ = "commands"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Phase 11.2: dispatch chain machinery so a chain like
    # polaris -> atlas -> hermes groups under a single id, has a
    # depth cap to prevent runaway loops, and supports a per-command
    # human-in-the-loop approval gate.
    #
    # source_agent: who dispatched this command. NULL when the
    # command was enqueued by a user (dashboard click) or by an
    # off-platform integration like the GitHub webhook receiver.
    # The constellation map's edges read from this column.
    source_agent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # dispatch_chain_id: groups every command in a single
    # cause-and-effect chain. Auto-generated when source_agent is
    # NULL; inherited from the active claim's chain when source_agent
    # is set (the SDK's threading.local from Phase 11.1).
    dispatch_chain_id: Mapped[str] = mapped_column(
        String, nullable=False, server_default="00000000-0000-0000-0000-000000000000"
    )
    # dispatch_depth: number of hops from the chain's root command.
    # Hard-capped per agent via Agent.max_dispatch_depth so a
    # buggy bot can't fork-bomb. Default 0 = chain root.
    dispatch_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # approval_state: human-in-the-loop gate. Commands stay 'pending'
    # until either (a) a user approves them via the dashboard, or
    # (b) a matching auto_approval_rule fires at enqueue time and
    # flips them to 'auto_approved'. claim_command only returns rows
    # in {'approved', 'auto_approved'}, so 'pending' commands sit
    # safely until acted on.
    approval_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    approved_by_user_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("idx_commands_ws_agent_status", "workspace_id", "agent_name", "status"),
        Index("idx_commands_chain", "dispatch_chain_id"),
        Index(
            "idx_commands_ws_source_recent",
            "workspace_id", "source_agent", "created_at",
        ),
    )


class CommandAutoApprovalRule(Base):
    """Phase 11.2: per-workspace rules that flip a command from
    `approval_state='pending'` to `'auto_approved'` at enqueue time.

    Lookup precedence on `(source_agent, target_agent, command_kind)`:
        1. Exact match on all three.
        2. Wildcard source ('*' source_agent + exact target + exact kind).
        3. Wildcard kind (exact source + exact target + '*' kind).
        4. No match -> command stays 'pending', waits on a human click.

    `mode='auto_approve'` flips to auto_approved; `mode='require_human'`
    is the explicit-deny path so a wildcard rule can be overridden by
    a more specific require_human entry.
    """

    __tablename__ = "command_auto_approval_rules"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_agent: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    target_agent: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    command_kind: Mapped[str] = mapped_column(
        String(128), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ValidatorConfig(Base):
    """Workspace-scoped registration of (event_kind, validator_name) -> config.

    The pipeline reads this on every POST /events to find which validators
    to run against the new event's payload. Composite PK is the natural
    upsert key for `PUT /workspaces/me/validators/{event_kind}/{validator_name}`.

    `mode` controls whether a fail result blocks ingestion or just tags
    the event:
      - "advisory" (default): the validator runs after insert, results
        land in event_validations as a chip on the dashboard. The event
        always lands.
      - "blocking": the validator runs before insert; on `fail` the API
        returns 422 with the violations and the event never lands.
    See Phase 8.2 for the pipeline integration.
    """
    __tablename__ = "validator_configs"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    validator_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # "advisory" | "blocking". Stored as a free string (not an enum) so
    # adding a new mode (e.g., "shadow" — run validator but log instead
    # of writing event_validations) is a code-only change.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="advisory"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_validator_configs_ws_kind", "workspace_id", "event_kind"
        ),
    )


class EventValidation(Base):
    """One row per (event, validator) pair recording the validation result.

    Status is a free string ('pass'|'fail'|'warn' today, 'timeout'|'error'
    once we wire those in). Violations is the list returned by the
    validator function — the same shape as ValidationResult.violations,
    persisted as JSONB so the dashboard can render whatever fields a
    given validator chose to attach.

    UNIQUE(event_id, validator_name) enforces "one row per event+validator";
    the pipeline's INSERT path is "best effort, ignore conflicts" so a
    re-run wouldn't double up rows.
    """
    __tablename__ = "event_validations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
    )
    validator_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    violations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id", "validator_name",
            name="uq_event_validations_event_validator",
        ),
        Index("idx_event_validations_event_id", "event_id"),
    )


class NotificationChannel(Base):
    """A registered destination for outbound notifications.

    One row per (workspace, channel name). The `type` field selects the
    formatter + dispatcher branch (Phase 9.2 ships slack/discord/teams/
    mattermost/webhook). `triggers` is a JSONB list of symbolic trigger
    names ('polaris.plan', 'validation.fail', 'run_failed') the channel
    cares about — the dispatcher matches against this on each event
    ingestion (Phase 9.4).

    `target_url` is user-supplied and treated as a secret: the API masks
    it before returning. `secret_token` is optional HMAC material for
    the generic webhook channel; native chat platforms don't use it
    (their URL is itself the credential).
    """
    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    triggers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    secret_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "name",
            name="uq_notification_channels_workspace_name",
        ),
        Index("idx_notification_channels_workspace", "workspace_id"),
    )


class NotificationDelivery(Base):
    """Audit row for one outbound notification attempt.

    Written by the test-fire endpoint (Phase 9.1) and the pipeline
    dispatcher (Phase 9.4) for every (channel, signal) pair. Status is
    a free string so a future delivery state ('queued', 'rate_limited')
    is a code-only change. `response_summary` carries whatever the
    HTTP-out captured: status code, response body snippet, error
    message, or — in the 9.1 stub path — `{"reason": "phase_9_2_will_deliver"}`.

    `event_id` is nullable because test-fires don't have a triggering
    event. ON DELETE SET NULL on the FK so an event purge doesn't
    cascade-delete the audit history.
    """
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_notification_deliveries_channel_sent",
            "channel_id", "sent_at",
        ),
    )


class GitHubIntegration(Base):
    """One workspace's connection to a GitHub repo.

    Phase 10.1 stores the repo identity, encrypted PAT, and encrypted
    webhook secret. Phase 10.2's webhook receiver looks rows up by
    repo full name; Phase 10.3's push-triggered redeploy uses the
    decrypted PAT to fetch tree + blobs from the GitHub Contents API.

    The webhook secret is generated server-side on first registration
    and revealed once in the PUT response so the user can paste it
    into GitHub's webhook config. After that it stays encrypted at
    rest and is consulted on every inbound webhook to verify the
    `X-Hub-Signature-256` header.

    `UNIQUE(workspace_id)` enforces one repo per workspace v1.
    """
    __tablename__ = "github_integrations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    repo_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    encrypted_pat: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", name="uq_github_integrations_workspace"
        ),
    )


class GitHubAgentPath(Base):
    """Maps (workspace, agent_name) → repo-relative path.

    A push that touches files under a registered path triggers a
    redeploy of that agent in Phase 10.3. Composite PK lets a
    workspace register multiple agents (one path per agent) without
    a join table.

    Path validation (no `..`, no leading slash, max 512 chars) lives
    at the endpoint layer rather than as a CHECK constraint so the
    error message can name the rule.
    """
    __tablename__ = "github_agent_paths"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GithubConnection(Base):
    """Phase 10B: workspace-level GitHub connection (the OAuth/PAT token).

    Replaces the per-repo token that lived on `github_integrations`. One
    connection per workspace holds the credential (an OAuth access token
    or a pasted PAT); the repos a workspace watches live in `github_repos`
    and reference this connection. Splitting the token from the repo is
    what lets one workspace watch many repos with a single auth.
    """

    __tablename__ = "github_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    auth_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pat"
    )  # 'oauth' | 'pat'
    # The GitHub login that authorized the OAuth grant; null for PAT.
    github_login: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GithubRepo(Base):
    """Phase 10B: one repo a workspace watches, under a GithubConnection.

    N rows per workspace (the multi-repo upgrade from the single
    `github_integrations` row). The webhook still routes by
    (repo_owner, repo_name); the per-repo webhook secret verifies the
    push signature, and the token comes from the parent connection.
    """

    __tablename__ = "github_repos"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("github_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    repo_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    encrypted_webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # One row per (workspace, repo). The webhook looks repos up by
        # (repo_owner, repo_name); this index also serves that probe.
        Index(
            "uq_github_repos_ws_owner_name",
            "workspace_id", "repo_owner", "repo_name",
            unique=True,
        ),
        Index("ix_github_repos_owner_name", "repo_owner", "repo_name"),
    )


class ModelPricing(Base):
    """Per-model token pricing. The `pricing.PRICING` literal is the source
    of truth; this table is a mirror that gets re-asserted on every
    `upgrade_to_head()` call (see `pricing.seed_model_pricing`). It exists
    so the dashboard can render a "current rates" table without re-shipping
    the SDK, and to set up the per-workspace override path in
    `WorkspacePricingOverride`.

    Cost computation in `cost.py` reads from `pricing.PRICING` directly —
    not from this table — to avoid a DB round-trip on every event ingest.
    """

    __tablename__ = "model_pricing"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    input_per_million_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    output_per_million_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deprecated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class WorkspacePricingOverride(Base):
    """Per-workspace pricing overrides for negotiated enterprise rates.

    Empty in v1 — the cost computation path does NOT consult this table
    yet. Reserved so future Phase 12+ work has a place to put per-workspace
    rates without another schema migration.
    """

    __tablename__ = "workspace_pricing_overrides"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    input_per_million_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    output_per_million_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GenerationJob(Base):
    """Async job row for long-running LLM calls (Phase 12C.6).

    `/agents/generate` and `/teams/plan` enqueue a row here instead of
    running their Anthropic call inline; the in-process runner in
    `backend/jobs.py` picks pending rows, dispatches by `kind`, and
    writes `result_payload` or `error` on terminal state. The dashboard
    polls `GET /workspaces/me/generation-jobs/{id}` to surface progress.

    `kind` is the dispatch discriminator. Valid values:
      - 'agent_generate': payload is the old POST /agents/generate body
      - 'team_plan': payload is the old POST /teams/plan body

    `status` lifecycle: pending -> running -> success | failed.
    No auto-retry in v1; on `failed`, the dashboard surfaces the error
    and the user retries from the UI.
    """

    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    result_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Runner's pending-picker hits (status, created_at). Keeping
        # status leftmost lets the same index serve "is anything
        # pending?" probes.
        Index("idx_generation_jobs_status_created", "status", "created_at"),
        Index("idx_generation_jobs_workspace", "workspace_id", created_at.desc()),
    )


class RunEvaluation(Base):
    """Judge-LLM verdict on a completed Run (Phase 14.2).

    One row per (run_id, judge_model) pair. The eval runner samples
    completed runs via `backend/eval_sampler.py`, asks the configured
    judge (claude-sonnet-4-6 in v1) for a verdict, and persists the
    answer here. Dashboard reads via /workspaces/me/agents/{name}/quality;
    12D.3's auto-tuner consumes the same data via lightsei.get_quality_signal().

    Cost (judge_cost_usd) lands on the `lightsei.system` synthetic
    agent through the eval runner's Run row, so the workspace's
    monthly budget gate covers judge spend the same way it covers
    generation spend.
    """

    __tablename__ = "run_evaluations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    judge_model: Mapped[str] = mapped_column(String(64), nullable=False)
    # 'good' | 'borderline' | 'bad'. Short string instead of an enum so
    # adding a future verdict ('unparseable', etc.) doesn't need a
    # migration.
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    judge_tokens_in: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    judge_tokens_out: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    judge_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_run_evaluations_ws_agent_created",
            "workspace_id", "agent_name", created_at.desc(),
        ),
        Index(
            "idx_run_evaluations_ws_verdict_created",
            "workspace_id", "verdict", created_at.desc(),
        ),
        # DB-level guard mirroring the sampler's "skip already-evaluated
        # runs" check. One verdict per (run, judge) pair; switching
        # judges later (e.g. Opus-as-judge for a re-eval cycle) is
        # allowed because judge_model is part of the key.
        Index(
            "idx_run_evaluations_run_judge",
            "run_id", "judge_model",
            unique=True,
        ),
    )


class RunBehavioralViolation(Base):
    """Phase 15.2: a layer-4 behavioral-rule violation on a Run.

    One row per (run_id, rule) violation detected by
    `backend/behavioral_rules.py` across a run's event stream: a loop,
    runaway token spend, or an escalating-permission pattern. Advisory in
    v1 (recorded + surfaced on the run/agent views, the run is not
    halted). The unique (run_id, rule) index means re-evaluating a run
    updates rather than duplicates a given rule's violation.
    """

    __tablename__ = "run_behavioral_violations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    # 'loop' | 'runaway_tokens' | 'escalating_permissions'. Short string,
    # not an enum, so a new rule is a code-only change.
    rule: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # 'warn' | 'block'
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_behavioral_ws_agent_created",
            "workspace_id", "agent_name", created_at.desc(),
        ),
        Index(
            "idx_behavioral_run_rule",
            "run_id", "rule",
            unique=True,
        ),
    )


# ---------- Phase 19.1: Slack chat surface schema ---------- #


class SlackWorkspace(Base):
    """Phase 19.1: one row per Slack workspace that has installed the
    Lightsei Slack app.

    Owns the encrypted bot OAuth token + the binding to the Lightsei
    workspace. `revoked_at` is set when the install is removed (we keep
    the row for audit; the partial-unique index below excludes revoked
    rows so a fresh install of the same Slack workspace works without
    manual cleanup).

    The bot token is encrypted via the same secrets_crypto helper used
    by `WorkspaceSecret.encrypted_value` — never logged, never returned
    in serializers, only decrypted when the backend needs to call Slack
    on behalf of a workspace.
    """

    __tablename__ = "slack_workspaces"

    # Slack's team_id is a stable string like 'T0123ABCD' — never reused
    # across reinstalls of the same workspace.
    slack_team_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lightsei_workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    team_name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Slack bot token, xoxb-... Encrypted at rest; the decrypt path lives
    # in slack_oauth (Phase 19.2). Surface checks: never log this column.
    bot_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # SET NULL on user delete so the audit trail survives a user
    # tear-down. Not load-bearing for any runtime path.
    installed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One Lightsei workspace per Slack workspace at a time, filtered
        # to non-revoked installs. Postgres partial-unique index.
        Index(
            "ix_slack_workspaces_lightsei_workspace_active",
            "lightsei_workspace_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class SlackChannel(Base):
    """Phase 19.1: one row per Slack channel the Lightsei app has been
    mentioned in.

    The operator-set `sensitivity_level` is what the Phase 19.4 chat
    orchestrator uses to filter which bots can be reached from this
    channel — same trust-zone semantics as `agents.sensitivity_level`,
    just applied to the channel side of the request boundary.

    `opted_in` defaults False: the Lightsei bot stays silent until the
    operator explicitly turns a channel on from the dashboard. Without
    this, every channel the bot got added to would receive responses,
    which is the opposite of the wedge story.
    """

    __tablename__ = "slack_channels"

    slack_team_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lightsei_workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    channel_name: Mapped[str] = mapped_column(String(256), nullable=False)
    sensitivity_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'internal'"),
        default=DEFAULT_SENSITIVITY_LEVEL,
    )
    opted_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # Chat orchestrator's primary query is "given Lightsei
        # workspace X + sensitivity Y, which channels are opted in?"
        Index(
            "ix_slack_channels_workspace_sensitivity",
            "lightsei_workspace_id", "sensitivity_level",
        ),
        # FK on (slack_team_id) → slack_workspaces.slack_team_id is
        # defined in alembic 0032's ForeignKeyConstraint. SQLAlchemy
        # doesn't need to redeclare it for ORM operations; the runtime
        # CASCADE on slack_workspaces revocation is enforced at the
        # DB level.
    )


class ConnectorInstallation(Base):
    """Phase 20.1: per-workspace per-connector OAuth-token holder for
    the integration-breadth surface.

    One row per active install. `encrypted_tokens` holds the
    access_token + refresh_token + expires_at as encrypted JSON via the
    same secrets_crypto path used by WorkspaceSecret + SlackWorkspace.

    The partial-unique index on `(workspace_id, connector_type) WHERE
    revoked_at IS NULL` enforces one active install per
    (workspace, connector_type) — a revoke-and-reinstall cycle works
    cleanly without manual cleanup of the prior row.
    """

    __tablename__ = "connector_installations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # Validated app-side against CONNECTOR_REGISTRY in
    # backend/connectors/__init__.py; keeping the column free-form means
    # adding a new connector doesn't require a migration.
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_tokens: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # The granted scopes (subset of default_scopes — OAuth providers
    # let users decline). JSONB matches the other ORM JSON columns
    # (Agent.capabilities, Run.payload, etc.); we never query into it,
    # just read the whole list when computing capabilities.
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    installed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    external_account_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One active install per (workspace, connector_type).
        Index(
            "ix_connector_installations_ws_type_active",
            "workspace_id", "connector_type",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Workspace-scoped browse query for the /integrations page.
        Index(
            "ix_connector_installations_workspace_installed_at",
            "workspace_id", "installed_at",
        ),
    )


class ConnectorOAuthPendingState(Base):
    """Phase 20.2: short-lived state store for the connector-install
    OAuth hop. Mirrors SlackOAuthPendingState shape but adds
    `connector_type` (so the callback knows which connector to bind
    the resulting install to) + `code_verifier` (PKCE handshake)."""

    __tablename__ = "connector_oauth_pending_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    installed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_after: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_connector_oauth_pending_states_expires_at",
            "expires_at",
        ),
    )


class SlackOAuthPendingState(Base):
    """Phase 19.2: short-lived state store for the Slack OAuth
    start → callback hop.

    Same shape as `OAuthPendingState` (Phase 17.3, Google OAuth) but
    without the PKCE `code_verifier` column — Slack's OAuth v2 flow
    doesn't use PKCE. Separate table keeps Google's per-row schema
    assumptions out of the Slack flow's way.
    """

    __tablename__ = "slack_oauth_pending_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    lightsei_workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    installed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Where the dashboard wanted the user to land post-callback.
    # Defaults to /integrations/slack in the handler if null.
    redirect_after: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_slack_oauth_pending_states_expires_at",
            "expires_at",
        ),
    )


class SlackEvent(Base):
    """Phase 19.1: idempotency log for inbound Slack events.

    Slack retries delivery (up to 3 times within an hour and again at
    longer intervals when a 5xx is received). Without this table we'd
    dispatch the same `app_mention` twice. The chat webhook (19.3)
    inserts on receive + ignores any duplicate.

    Keyed on `(slack_team_id, event_id)` — event_ids are unique within
    a Slack team but tenant-isolating is cheap defense.

    Rows aged out by a cron (not in this sub-task). The `received_at`
    index makes that cleanup cheap.
    """

    __tablename__ = "slack_events"

    slack_team_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_slack_events_received_at",
            "received_at",
        ),
    )


# ---------- Phase 21.1: widget chat surface schema ---------- #


class WidgetConversation(Base):
    """Phase 21.1: one row per widget chat session.

    Anonymous-only in v1 — the `anon_user_id` is an opaque string the
    widget iframe stamps on localStorage so a returning visitor on
    the same site sees their previous conversation. Phase 21B adds
    signed-token identity which would land in a separate column on
    this same row.

    `customer_facing_agent_name` is snapshotted at conversation-start
    so renaming the bot later doesn't break the thread. The live
    pointer for "who answers NEW conversations right now" lives on
    `workspaces.customer_facing_agent_name`.

    Status machine: open → escalated (bot called escalate) → resolved
    (operator marked it done). operator_owned is a parallel state:
    the operator clicked Take Over in /inbox so the bot is paused
    and operators type replies directly.
    """

    __tablename__ = "widget_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    customer_facing_agent_name: Mapped[str] = mapped_column(
        String(128), nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=text("'open'"),
        default=DEFAULT_WIDGET_CONVERSATION_STATUS,
    )
    anon_user_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    # Phase 25.1: identified end-user pointer. NULL = anonymous
    # (legacy / opt-out path; `anon_user_id` carries the identity).
    # Non-NULL = the widget request authed as this end user; 25.4
    # filters conversation queries by this column when set. SET NULL
    # on end_user delete so the conversation history sticks around
    # for the vendor's audit purposes even if the end user account
    # is removed.
    end_user_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("end_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        # Inbox list query: filter by workspace + status, newest
        # activity first. Covers the /inbox + filter combo without
        # a separate scan per filter button.
        Index(
            "ix_widget_conversations_workspace_status_active",
            "workspace_id", "status", text("last_message_at DESC"),
        ),
        # Anon-user lookup for "did this end user have a previous
        # conversation on this workspace?" Partial-where keeps the
        # index small (only conversations with an anon id).
        Index(
            "ix_widget_conversations_workspace_anon_user",
            "workspace_id", "anon_user_id",
            postgresql_where=text("anon_user_id IS NOT NULL"),
        ),
        # Phase 25.1: identified-end-user lookup. Same partial-index
        # pattern as the anon-user lookup above; most v1 conversations
        # are still anonymous, so the partial keeps the index small.
        Index(
            "ix_widget_conversations_workspace_end_user",
            "workspace_id", "end_user_id",
            postgresql_where=text("end_user_id IS NOT NULL"),
        ),
    )


class WidgetMessage(Base):
    """Phase 21.1: one row per chat message in a widget conversation.

    Role enum (user / bot / operator / system) — system rows are
    framework-emitted events like "Operator joined" or "Bot
    escalated this conversation."

    Text body is unbounded at the DB layer; the request handler
    enforces a sensible cap before insert.
    """

    __tablename__ = "widget_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("widget_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    __table_args__ = (
        # Thread-render query: messages in this conversation,
        # oldest first. Also drives the widget poll-since-cursor
        # endpoint via `id > since`.
        Index(
            "ix_widget_messages_conversation_sent_at",
            "conversation_id", "sent_at",
        ),
    )


class WidgetEscalation(Base):
    """Phase 21.1: one row per escalation event on a conversation.

    Bot raising `LightseiEscalate` (Phase 21.5), an operator
    flipping the conversation to escalated, or a bot handler
    crashing — each lands as a row here with a `reason` keyword
    + a free-form `payload` for context.

    `suggested_fix` stays null until Phase 21.9's Polaris
    incident-response extension clusters similar escalations and
    proposes a fix. Shape when populated:
    `{kind: 'system_prompt_addendum' | 'add_faq_entry',
       detail: <markdown or json>}`.

    Resolving an escalation (operator marks the conversation done)
    stamps `resolved_at` + optionally `resolved_by_user_id`. The
    row lives on for audit even after resolve.
    """

    __tablename__ = "widget_escalations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("widget_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    suggested_fix: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True,
    )
    escalated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    resolved_by_user_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    __table_args__ = (
        # Polaris's pattern-detection query (21.9): the last N
        # hours of unresolved escalations, newest first. Partial
        # index keeps it tight.
        Index(
            "ix_widget_escalations_open_recent",
            text("escalated_at DESC"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )


class Trigger(Base):
    """Phase 22.1: one row per configured trigger on an agent.

    Two kinds in v1: 'cron' (recurring schedule, scheduler loop in
    worker fires on next_run_at) and 'webhook' (token in URL, fired
    by external POST /triggers/{token}/fire). Event-based kinds
    (Gmail label, Drive change, Calendar event tagged) are parked to
    Phase 22B.

    Agent reference is `agent_name: String` not a real FK because
    the agents table has a composite PK (workspace_id, name). Same
    snapshot pattern as Deployment + WidgetConversation; app-side
    queries resolve via the (workspace_id, agent_name) pair.

    Cron rows: `schedule` is required + parsed via croniter,
    `next_run_at` is pre-computed so the scheduler's hot query is a
    simple WHERE filter. Webhook rows: `webhook_token_hash` is the
    sha256 of the plaintext token (returned to the operator once at
    create-time), partial-unique so multiple cron rows coexist.
    `next_run_at` is NULL for webhook rows.

    `last_run_id` is a real FK (runs.id is single-column). SET NULL
    on run delete so the trigger survives run cleanup.
    `last_run_status` is a snapshot of the last run's status, kept
    on this row so the dashboard list renders without a JOIN.
    """

    __tablename__ = "triggers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
    )
    webhook_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
        default=True,
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_run_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    __table_args__ = (
        # Scheduler's hot query: enabled cron triggers due to fire,
        # ordered by next_run_at. Partial-where keeps the index
        # small (webhook rows always have NULL next_run_at + don't
        # need the scheduler to look at them).
        Index(
            "ix_triggers_due",
            "enabled", "next_run_at",
            postgresql_where=text("kind = 'cron'"),
        ),
        # Per-agent triggers list (dashboard panel in 22.7).
        Index(
            "ix_triggers_workspace_agent",
            "workspace_id", "agent_name",
        ),
        # Webhook token lookup. Partial-unique: multiple cron rows
        # (NULL hash) coexist, only populated hashes must be unique.
        Index(
            "ix_triggers_webhook_token",
            "webhook_token_hash",
            unique=True,
            postgresql_where=text("webhook_token_hash IS NOT NULL"),
        ),
    )


class EndUser(Base):
    """Phase 25.1: a person who buys from a Lightsei-using business.

    Distinct from `User` (the operator entity). Operators configure
    bots and view runs; end users chat with bots through `/c` (Phase
    26) and the identified widget path (Phase 25.4). The two share
    no rows: an operator who is also a customer of another vendor
    on Lightsei would have one `User` row and one `EndUser` row,
    keyed by separate emails or even the same email.

    `auth_provider` is `magic_link` in v1; Apple / Google OAuth are
    parked to 25B. `email_verified` flips to True after the magic
    link round-trip; signups without a verified address are not
    minted (the consume path verifies before insert).
    """

    __tablename__ = "end_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
    )
    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
    )
    auth_provider: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'magic_link'"),
        default=DEFAULT_END_USER_AUTH_PROVIDER,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class EndUserSession(Base):
    """Phase 25.1: bearer-token session for an end user.

    Parallel to the operator `Session` table but keyed off `EndUser`.
    Resolved by `backend/end_user_auth.py` (Phase 25.3); the token
    plaintext is what the `/c` cookie + the identified widget bearer
    header carry, the DB only ever sees the sha256 in `token_hash`.

    CASCADE on end_user delete: when an end user account goes away
    (25B self-service deletion or admin removal), every active
    session goes with it.
    """

    __tablename__ = "end_user_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    end_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(
        String, unique=True, nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index("ix_end_user_sessions_end_user", "end_user_id"),
    )


class EndUserVendorLink(Base):
    """Phase 25.1: a "customer of" relationship between an end user
    and a workspace (vendor).

    Composite PK on (end_user_id, workspace_id) so the same end user
    can be linked to many vendors and the same vendor can have many
    linked end users. v1 only inserts via invite-code redemption
    (25.2 + 27.2). `removed_at` is a soft-revoke pointer: past
    conversations stay readable to the end user, but no new messages
    can be sent on a removed link.

    Both FKs CASCADE. Deleting the workspace removes the relationship
    cleanly; deleting the end user removes all their subscriptions.
    """

    __tablename__ = "end_user_vendor_links"

    end_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    linked_via: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'invite_code'"),
        default=DEFAULT_END_USER_VENDOR_LINK_VIA,
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Phase 27.1: optional per-vendor display name. The end user can
    # show as "Alice Smith" to vendor A but "alice@example.com" to
    # vendor B without forking the EndUser row. NULL = fall back to
    # `end_users.display_name`.
    display_name_override: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
    )
    # Phase 27.1: per-vendor push notification preference. Phase 28's
    # push delivery reads this before sending. 'all' = every reply,
    # 'mentions' = only when bot @-mentions the end user (future
    # hook), 'off' = never. Default 'all' per Phase 27 spec.
    notification_pref: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'all'"),
        default=DEFAULT_NOTIFICATION_PREF,
    )

    __table_args__ = (
        # Per-workspace roster: "show me every end user linked to
        # this vendor, oldest first." The PK already covers per-end-
        # user lookup.
        Index(
            "ix_end_user_vendor_links_workspace",
            "workspace_id", "linked_at",
        ),
    )


class VendorInviteCode(Base):
    """Phase 27.1: single-use, time-bound code an operator mints so
    an end user can claim a vendor link.

    The `code` value is also the primary key and the literal string
    the operator hands to the end user (e.g. via a generated URL or
    a copy-paste-able token shown once on the workspace settings
    page). UUID-shaped to be unguessable; single-use because
    `consumed_at` getting set is irreversible.

    `consumed_by_end_user_id` is an audit pointer (SET NULL on end-
    user delete). The actual link row that grants chat access lives
    in `end_user_vendor_links`; this table is the bookkeeping ledger
    of which codes were issued + redeemed.

    No partial unique on `(workspace_id, code)` because `code` is
    PK + therefore unique across all workspaces, which is the
    looser-but-also-fine guarantee.
    """

    __tablename__ = "vendor_invite_codes"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    consumed_by_end_user_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("end_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_vendor_invite_codes_workspace_created",
            "workspace_id", text("created_at DESC"),
        ),
    )


class EndUserSigninToken(Base):
    """Phase 25.1: single-use magic-link token for end-user signup
    + signin.

    Same shape as the operator `EmailSigninToken` from Phase 17: the
    plaintext goes in the magic-link URL, the DB only stores the
    sha256 hash, an audit-trail `consumed_at` marks use instead of
    deletion.

    The request side stores `email` (not `end_user_id`) because the
    request path is also signup: at request time there is not
    necessarily an `EndUser` row yet. The consume path either
    matches an existing end_user by email or creates one in the
    same transaction.

    `vendor_invite_code` carries an optional invite code through
    the email round-trip so the consume path can link the freshly-
    minted end_user to a vendor without a second hop.
    """

    __tablename__ = "end_user_signin_tokens"

    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    vendor_invite_code: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )

    __table_args__ = (
        # Rate-limit + active-token probe, same shape as the operator
        # email_signin_tokens index.
        Index(
            "ix_end_user_signin_tokens_email_created",
            "email", text("created_at DESC"),
        ),
    )


class EndUserPushSubscription(Base):
    """Phase 28.1: web-push subscription for one (end_user, device)
    pair.

    Stored on `PushManager.subscribe()` in the browser. The Phase
    28.2 send helper fans out across active rows (revoked_at IS NULL)
    for a given end_user; on 410 Gone from the push service, it
    sets `revoked_at` so the row is skipped next time.

    Composite unique on `(end_user_id, endpoint)`: a re-subscribe
    from the same device updates an existing row rather than creating
    duplicates. The Phase 28.5 subscribe endpoint uses upsert.

    `last_used_at` is bumped on successful send so an audit can
    distinguish active devices from stale rows.
    """

    __tablename__ = "end_user_push_subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    end_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "end_user_id", "endpoint",
            name="uq_end_user_push_subscriptions_end_user_endpoint",
        ),
        # Partial index for the fan-out hot path: every push event
        # scans WHERE end_user_id = ? AND revoked_at IS NULL.
        Index(
            "ix_end_user_push_subscriptions_active",
            "end_user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class EndUserApnsToken(Base):
    """Phase 29.4: APNS device token for one (end_user, device) pair.

    Parallel to EndUserPushSubscription but keyed on Apple's APNS
    device tokens. The Phase 29.4 send helper fans out across active
    rows for an end user; on 410 BadDeviceToken or 410 Unregistered,
    it sets `revoked_at` so the row is skipped next time.

    Composite unique on `(end_user_id, device_token)` for the
    re-register upsert (iOS tokens rotate ~monthly).

    `bundle_id` + `environment` keep prod / TestFlight / dev tokens
    distinguishable so the sender hits the right APNS topic + gateway.
    """

    __tablename__ = "end_user_apns_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    end_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_token: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "end_user_id", "device_token",
            name="uq_end_user_apns_tokens_end_user_token",
        ),
        Index(
            "ix_end_user_apns_tokens_active",
            "end_user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
