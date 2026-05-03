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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Phase 11B.1: workspace-level monthly spend cap. NULL = no cap.
    # When set and reached, runs in this workspace get denied with the same
    # UX path as Phase 2's per-agent daily cap.
    budget_usd_monthly: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True
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

    __table_args__ = (
        Index("idx_runs_started_at", started_at.desc()),
        Index("idx_runs_ws_started_at", "workspace_id", started_at.desc()),
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

    __table_args__ = (
        Index("idx_users_workspace", "workspace_id"),
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

    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
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
