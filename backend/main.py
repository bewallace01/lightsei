import hashlib
import hmac
import json
import os
import secrets as _stdlib_secrets
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

import behavioral_rules
import limits
import policies
import secrets_crypto
from auth import AuthResult, get_authenticated, get_workspace_id
from end_user_auth import EndUserAuthResult, get_end_user
from cost import (
    add_run_cost_from_event,
    agent_cost_since,
    utc_day_start,
    workspace_cost_mtd,
)
from pricing import compute_cost_usd
from db import ensure_agent, get_session, session_scope
from validator_defaults import seed_default_validators
from limits import (
    BodySizeLimitMiddleware,
    limit_login_attempt,
    limit_signup_attempt,
)
from keys import (
    generate_key,
    generate_session_token,
    hash_token,
    prefix_for_display,
)
from migrate import upgrade_to_head
from models import (
    Agent,
    AgentInstance,
    ApiKey,
    Command,
    CommandAutoApprovalRule,
    Deployment,
    DeploymentBlob,
    DeploymentLog,
    EmailSigninToken,
    EndUser,
    EndUserApnsToken,
    EndUserPushSubscription,
    EndUserSession,
    EndUserSigninToken,
    EndUserVendorLink,
    Event,
    EventValidation,
    GenerationJob,
    GitHubAgentPath,
    GitHubIntegration,
    GithubConnection,
    GithubRepo,
    GithubRepoBranchTarget,
    NotificationChannel,
    ConnectorInstallation,
    ConnectorOAuthPendingState,
    NotificationDelivery,
    OAuthPendingState,
    Run,
    RunBehavioralViolation,
    SlackChannel,
    SlackEvent,
    SlackOAuthPendingState,
    SlackWorkspace,
    Session as SessionRow,
    TeamConversation,
    TeamMessage,
    Thread,
    ThreadMessage,
    Trigger,
    User,
    ValidatorConfig,
    WidgetConversation,
    WidgetEscalation,
    WidgetMessage,
    Workspace,
    WorkspaceMember,
    WorkspaceSecret,
    is_valid_notification_pref,
    is_valid_trigger_kind,
    is_valid_vendor_slug,
    VendorInviteCode,
)
import github_api
import github_oauth
import notifications
import validators
from notifications import triggers as notification_triggers
from validation_pipeline import (
    evaluate_validators,
    find_blocking_failures,
    write_validation_rows,
)
from worker_auth import get_worker
from passwords import hash_password, verify_password

SESSION_TTL = timedelta(days=30)
COMMAND_TTL = timedelta(hours=24)
# An instance is "active" if we heard from it within this window. Tuned for a
# default 30s SDK heartbeat with two missed beats of slack.
INSTANCE_ACTIVE_WINDOW = timedelta(seconds=90)
# Cap on concurrently-active instances of the same agent from a single
# hostname. Stops the runaway-process pattern where someone leaves
# `python polaris/bot.py` running in 25 detached terminal tabs and each
# one independently bills Anthropic. The 26th refuses to register and
# the SDK exits with a clear message rather than silently overlapping.
# Override per-deployment with LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME.
MAX_INSTANCES_PER_HOSTNAME = int(
    os.getenv("LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME", "3")
)
# A worker that hasn't heartbeated for this long loses its claim — any other
# worker can re-claim the deployment. Tuned for a 30s worker heartbeat.
WORKER_CLAIM_TTL = timedelta(seconds=120)
# Max upload bytes for a deployment zip. Mirrors limits.MAX_UPLOAD_BYTES so
# the per-route check matches the middleware's cap.
MAX_DEPLOYMENT_BLOB_BYTES = 10 * 1024 * 1024
# A single deployment keeps the most recent N log lines on the server.
# Older lines are pruned on insert.
MAX_DEPLOYMENT_LOG_LINES = 1000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventIn(BaseModel):
    run_id: str
    agent_name: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class PolicyCheckIn(BaseModel):
    agent_name: Optional[str] = None
    run_id: Optional[str] = None
    action: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentPatchIn(BaseModel):
    daily_cost_cap_usd: Optional[float] = None  # null clears the cap
    system_prompt: Optional[str] = None  # null clears the prompt
    # Phase 12.1: per-agent LLM provider + model pin. null clears.
    # Provider validated against the small enum below at handler time.
    provider: Optional[str] = None
    model: Optional[str] = None
    # Per-agent tick interval (seconds). null = use the bot's env default.
    # Validated against the bounds below at handler time so a typo in the
    # dashboard doesn't melt budget by setting a 1-second tick.
    tick_interval_s: Optional[int] = None
    # Short freeform description shown on the /agents roster. null clears.
    description: Optional[str] = Field(default=None, max_length=2000)
    # Phase 16.1: trust-zone sensitivity. Validated against
    # _VALID_SENSITIVITY_LEVELS in the handler.
    sensitivity_level: Optional[str] = None
    # Phase 16.4: opt-in for cross-zone dispatch. None = leave unchanged;
    # True/False updates the column.
    dispatches_cross_zone: Optional[bool] = None
    # Distinguish "field not provided" from "explicitly null". Pydantic v2:
    # we'll detect via model_fields_set.


# Tick-interval bounds. 60s lower bound: anything tighter will rate-limit
# against vendor LLM APIs and burn budget. 86400s (24h) upper bound: longer
# intervals than that should use a different mechanism (cron, scheduled
# routine), not a tick loop.
AGENT_TICK_INTERVAL_MIN_S = 60
AGENT_TICK_INTERVAL_MAX_S = 86400


# Providers Lightsei has (or plans to have, in 12.2/12.3) an SDK adapter for.
# Validating at the API layer rather than a DB CHECK constraint so a future
# adapter ships without a schema migration. Keeping the set tight on input
# prevents typos like "antropic" from silently sticking.
SUPPORTED_PROVIDERS: set[str] = {
    "openai",
    "anthropic",
    "google",
    "groq",
    "xai",
    "cohere",
}


class WorkspaceCreateIn(BaseModel):
    name: str
    api_key_name: str = "default"


class WorkspacePatchIn(BaseModel):
    # name and budget are independently optional — clients can patch one
    # without touching the other. Pydantic v2's `model_fields_set`
    # distinguishes "client didn't send this" from "client sent null."
    name: Optional[str] = Field(default=None, min_length=1)
    # NULL means "clear the cap" when the client explicitly passes null;
    # the patch handler reads `model_fields_set` to disambiguate.
    budget_usd_monthly: Optional[float] = None
    # Phase 21.9: opt in to auto-applying Polaris's suggested fixes
    # to the customer-facing bot's system_prompt without operator
    # review. Off by default; flipping requires explicit consent.
    polaris_auto_apply_widget_fixes: Optional[bool] = None


# Phase 26.1: vendor-slug claim body. Slug format is re-validated
# server-side via models.is_valid_vendor_slug; the pydantic field
# length cap is the cheap pre-filter.
class VendorSlugIn(BaseModel):
    slug: str = Field(min_length=3, max_length=32)


# Phase 27.2: vendor invite mint body. Operator picks how many codes
# to issue + optional TTL override (default 30 days). Caps at 100 so
# a misclick doesn't flood the table with a million codes.
class VendorInviteMintIn(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    ttl_days: int = Field(default=30, ge=1, le=365)


# Phase 27.2: end-user redeem body. Code is the literal value the
# operator handed the end user.
class VendorInviteRedeemIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


# Phase 27.2: end-user updates their per-vendor settings. Both
# fields independently optional via Pydantic v2 model_fields_set
# pattern (same shape as WorkspacePatchIn).
class EndUserVendorPatchIn(BaseModel):
    notification_pref: Optional[str] = None
    # NULL clears the override (falls back to end_users.display_name);
    # empty string is also treated as a clear.
    display_name_override: Optional[str] = Field(
        default=None, max_length=128,
    )


# Phase 28.5: body shape from PushManager.subscribe().toJSON() — the
# browser hands the backend exactly these three fields (endpoint URL
# unique to the push service, p256dh + auth keys used by pywebpush
# to encrypt payloads). The composite unique constraint on the
# end_user_push_subscriptions table is (end_user_id, endpoint), so
# re-POSTing the same endpoint upserts the row.
class EndUserPushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


# Phase 28.5: DELETE body — the browser hands back the endpoint that
# came from PushManager.unsubscribe(), the backend uses it to find
# the matching row + set revoked_at.
class EndUserPushUnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


# Phase 29.2c stub: Sign in with Apple body. `identity_token` is
# the JWT the iOS app gets from ASAuthorizationAppleIDCredential.
# `email` + `display_name` only arrive on the FIRST sign-in (per
# Apple docs); the iOS app caches them locally + forwards on
# subsequent calls so the backend can keep its row correct.
class EndUserSignInWithAppleIn(BaseModel):
    identity_token: str = Field(min_length=1, max_length=4096)
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(default=None, max_length=128)


# Phase 29.4 stub: APNS device-token register/unregister bodies.
# `device_token` is hex-encoded (~64 chars today). `bundle_id` +
# `environment` let the sender pick the right APNS topic + gateway
# in case TestFlight + App Store builds coexist.
class EndUserApnsRegisterIn(BaseModel):
    device_token: str = Field(min_length=1, max_length=256)
    bundle_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(pattern=r"^(sandbox|production)$")


class EndUserApnsUnregisterIn(BaseModel):
    device_token: str = Field(min_length=1, max_length=256)


class ApiKeyCreateIn(BaseModel):
    name: str = "default"


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    workspace_name: str = Field(min_length=1)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


# Phase 17.2: magic-link auth.
class MagicLinkRequestIn(BaseModel):
    email: EmailStr


class MagicLinkConsumeIn(BaseModel):
    token: str = Field(min_length=8, max_length=256)


# Phase 25.2: end-user magic-link auth. Distinct from the operator
# pair above because end users have a separate identity surface
# (end_users vs users) + the request body optionally carries a
# vendor_invite_code through the email round trip.
class EndUserMagicLinkRequestIn(BaseModel):
    email: EmailStr
    # Optional invite code the end user typed during signup. Stored
    # on the signin token row in 25.1's schema; Phase 27.2 consumes
    # it to link end_user → workspace after vendor_invite_codes
    # lands. Carried through unchanged in 25.2.
    vendor_invite_code: Optional[str] = Field(
        default=None, min_length=1, max_length=64,
    )


class EndUserMagicLinkConsumeIn(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    # Optional invite code the end user typed AFTER landing on the
    # consume page (if they didn't include one in the original
    # request). Lets the dashboard surface "I have an invite code"
    # at consume time too. Phase 27.2 uses this same field; today
    # it's accepted, carried back on the response, but not acted
    # on.
    vendor_invite_code: Optional[str] = Field(
        default=None, min_length=1, max_length=64,
    )


class CommandEnqueueIn(BaseModel):
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Phase 11.2: dispatch chain wiring. The SDK in 11.1 already
    # sends dispatch_chain_id on every send_command call (Pydantic
    # silently dropped it before this phase); now the server reads
    # it. source_agent identifies the dispatcher when present —
    # NULL means "enqueued by a user / off-platform integration"
    # which is fine and bypasses the depth + per-day caps because
    # the runaway risk only exists for agent-driven chains.
    dispatch_chain_id: Optional[str] = None
    source_agent: Optional[str] = None


class CommandApprovalIn(BaseModel):
    """Phase 11.2: shape for POST /commands/{id}/approve and /reject.
    `reason` is optional free-text recorded on the audit trail. The
    handler differentiates approve vs reject by the path, not the
    payload — keeps the action explicit at the URL."""
    reason: Optional[str] = Field(default=None, max_length=500)


class AutoApprovalRuleIn(BaseModel):
    source_agent: str = Field(min_length=1, max_length=64)
    target_agent: str = Field(min_length=1, max_length=64)
    command_kind: str = Field(min_length=1, max_length=128)
    mode: str = Field(pattern="^(auto_approve|require_human)$")


class NotificationDispatchIn(BaseModel):
    """Phase 11.4: Hermes uses this endpoint to fan out arbitrary
    text from other agents into the workspace's configured channels."""
    channel_name: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    severity: str = Field(
        default="info", pattern="^(info|error|warning)$"
    )


class CommandCompleteIn(BaseModel):
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class AgentManifestIn(BaseModel):
    command_handlers: list[dict[str, Any]] = Field(default_factory=list)


class AgentGenerateIn(BaseModel):
    """Input to `POST /workspaces/me/agents/generate`.

    `description` (Phase 12B.1) is the user's natural-language prompt — what
    the bot should do. `target_agents` is an optional whitelist of existing
    agents to encourage coordination with (the LLM still sees the full
    workspace constellation either way; this is a hint). `name_hint` lets
    the user nudge toward a specific star-dictionary name, which is honored
    if the name fits the role; otherwise the LLM picks.

    Phase 12B.3 iteration loop: when `tweak_request` is set alongside
    `previous_bot_py` and `previous_requirements_txt`, the endpoint sends
    a follow-up turn that includes the prior generation + the tweak so
    Claude can refine rather than start over.
    """
    description: str = Field(min_length=8, max_length=4000)
    target_agents: Optional[list[str]] = None
    name_hint: Optional[str] = None
    # Iteration loop fields (12B.3). All three set together → refinement.
    tweak_request: Optional[str] = Field(default=None, max_length=4000)
    previous_bot_py: Optional[str] = Field(default=None, max_length=200_000)
    previous_requirements_txt: Optional[str] = Field(default=None, max_length=20_000)


class TeamPlanIn(BaseModel):
    """Input to `POST /workspaces/me/teams/plan` (Phase 12C.1).

    At least one of `readme_text`, `freeform_description`, or
    `github_repo` must be set. When `github_repo` is set, the endpoint
    fetches the README server-side via the workspace's GitHub
    integration. `github_branch` defaults to the integration's default
    branch (typically `main`).
    """
    readme_text: Optional[str] = Field(default=None, max_length=200_000)
    freeform_description: Optional[str] = Field(default=None, max_length=10_000)
    github_repo: Optional[str] = Field(default=None, max_length=200)
    github_branch: Optional[str] = Field(default=None, max_length=200)


class ThreadCreateIn(BaseModel):
    title: Optional[str] = None


class ThreadMessagePostIn(BaseModel):
    content: str = Field(min_length=1)


class TeamConversationCreateIn(BaseModel):
    title: Optional[str] = None


class TeamMessagePostIn(BaseModel):
    content: str = Field(min_length=1)


class ThreadMessageCompleteIn(BaseModel):
    content: Optional[str] = None
    error: Optional[str] = None


class ThreadMessageChunkIn(BaseModel):
    delta: str


class InstanceHeartbeatIn(BaseModel):
    instance_id: str = Field(min_length=1)
    hostname: Optional[str] = None
    pid: Optional[int] = None
    sdk_version: Optional[str] = None
    started_at: Optional[datetime] = None


# Secret names look like env vars: ASCII letter, then letters/digits/underscores.
# Cap at 64 chars so the URL stays sane and the index is small.
import re as _re
SECRET_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class SecretSetIn(BaseModel):
    value: str = Field(min_length=0, max_length=8192)


class ValidatorConfigSetIn(BaseModel):
    """PUT /workspaces/me/validators/{event_kind}/{validator_name} body.

    `config` is opaque to the API — its shape is validator-specific
    (schema-strict expects `{schema: ...}`, content-rules expects
    `{rules: [...]}`) and the validator function is what defines the
    contract. We just store and forward.

    `mode` defaults to "advisory" so a Phase 7A caller that omits the
    field gets the existing behavior. "blocking" opts the validator
    into pre-emit rejection (Phase 8.2 wires that path).
    """
    config: dict[str, Any] = Field(default_factory=dict)
    mode: str = "advisory"


# Validator-name and event-kind validation: same character class as
# secret names since these strings end up in URL paths and DB columns.
VALIDATOR_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EVENT_KIND_RE = _re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
VALIDATOR_MODES = ("advisory", "blocking")


# ---------- /workspaces/me/github (Phase 10.1) ---------- #


# GitHub repo identity validation. The GitHub username/repo regex per
# their docs allows [A-Za-z0-9._-], 1-39 chars for owner, up to 100 for
# repo. We're a touch stricter on length to match column widths and
# keep error messages tidy.
GITHUB_OWNER_RE = _re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]){0,38}$")
GITHUB_REPO_RE = _re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]){0,99}$")
GITHUB_BRANCH_RE = _re.compile(r"^[A-Za-z0-9._/\-]{1,255}$")
# Same agent-name shape used in the rest of the API.
GITHUB_AGENT_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


# Built once on module load so registration response builders don't
# re-do the env lookup. Same env var convention as Phase 9's
# DASHBOARD_BASE_URL.
GITHUB_WEBHOOK_BASE_URL = os.environ.get(
    "LIGHTSEI_API_BASE_URL", "https://api.lightsei.com"
).rstrip("/")


class GitHubIntegrationSetIn(BaseModel):
    """PUT /workspaces/me/github body.

    On first registration, the user provides repo + branch + PAT. The
    server generates the webhook_secret server-side (so a PAT leak
    doesn't compromise inbound-webhook authentication too) and reveals
    it once in the response. After that the secret stays encrypted at
    rest.

    Subsequent PUTs (update path: rotate the PAT, change the branch,
    move to a different repo) replace the corresponding fields.
    Webhook secret is not regenerated by default — that's a separate
    rotate operation when we want it.
    """
    repo_owner: str = Field(min_length=1, max_length=255)
    repo_name: str = Field(min_length=1, max_length=255)
    branch: str = Field(default="main", min_length=1, max_length=255)
    # PAT is plaintext on the wire and never echoed back; we encrypt
    # before storing.
    pat: str = Field(min_length=1, max_length=512)


class GitHubRepoAddIn(BaseModel):
    """POST /workspaces/me/github/repos body (Phase 10B.2).

    Registers a repo under the workspace's existing GitHub connection
    (the token comes from the connection, not the body). The server
    generates the per-repo webhook secret and reveals it once.
    """
    repo_owner: str = Field(min_length=1, max_length=255)
    repo_name: str = Field(min_length=1, max_length=255)
    branch: str = Field(default="main", min_length=1, max_length=255)


class GitHubBranchTargetAddIn(BaseModel):
    """POST .../repos/{repo_id}/branch-targets (Phase 10B.4).

    Map a branch to an agent that deploys when that branch is pushed.
    """
    branch: str = Field(min_length=1, max_length=255)
    agent_name: str = Field(min_length=1, max_length=255)


class GitHubAgentPathSetIn(BaseModel):
    """PUT /workspaces/me/github/agents/{agent_name} body.

    `path` is repo-relative. Forward slashes for nested dirs. No
    leading slash, no `..` segments — validated below in
    _validate_github_path.
    """
    path: str = Field(min_length=1, max_length=512)


def _validate_github_path(path: str) -> None:
    """No leading slash, no `..` segments, no whitespace-only."""
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="path must not be blank")
    if path.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="path is repo-relative; remove the leading slash",
        )
    # Block `..` anywhere it could be a path segment. Conservative —
    # we'd rather reject `foo..bar` (probably fine but uncommon) than
    # let a `../` slip through.
    if ".." in path.split("/"):
        raise HTTPException(
            status_code=400,
            detail="path may not contain `..` segments",
        )
    if "\\" in path:
        raise HTTPException(
            status_code=400,
            detail="use forward slashes for nested paths, not backslashes",
        )


def _generate_webhook_secret() -> str:
    """40-char URL-safe random string. GitHub allows arbitrary
    secret strings; we keep ours alphanumeric for paste-friendliness."""
    return _stdlib_secrets.token_urlsafe(30)  # ~40 chars after b64


def _mask_pat(plaintext: str) -> str:
    """Display-time mask. Keep first 4 + last 4 so the user can
    visually confirm "yes, this is the token I added" without seeing
    the secret bytes."""
    if len(plaintext) <= 12:
        return "***"
    return f"{plaintext[:4]}...{plaintext[-4:]}"


def _serialize_github_integration(
    g: "GitHubIntegration",
    *,
    pat_plaintext: Optional[str] = None,
    webhook_secret_plaintext: Optional[str] = None,
) -> dict[str, Any]:
    """Standard GET response shape. PAT is masked unless
    `pat_plaintext` is passed (we never pass it on GET — only on the
    initial PUT response, and even there we mask). Webhook secret
    `webhook_secret_plaintext` is included only on first creation
    so the user can paste it into GitHub; it's omitted on subsequent
    GETs to avoid leaving it lying around in browser history."""
    masked = _mask_pat(pat_plaintext) if pat_plaintext else "***"
    out: dict[str, Any] = {
        "id": g.id,
        "repo_owner": g.repo_owner,
        "repo_name": g.repo_name,
        "branch": g.branch,
        "pat_masked": masked,
        "webhook_url": f"{GITHUB_WEBHOOK_BASE_URL}/webhooks/github",
        "has_webhook_secret": True,
        "is_active": g.is_active,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
    }
    if webhook_secret_plaintext is not None:
        # Revealed exactly once, on the first registration. The
        # explicit `.webhook_secret` key signals to clients that this
        # is the only chance to capture it.
        out["webhook_secret"] = webhook_secret_plaintext
        out["webhook_secret_reveal_note"] = (
            "Save this secret now — it is shown once. To rotate, "
            "DELETE the integration and re-register."
        )
    return out


def _serialize_github_agent_path(p: "GitHubAgentPath") -> dict[str, Any]:
    return {
        "agent_name": p.agent_name,
        "path": p.path,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


def _serialize_api_key(k: ApiKey) -> dict[str, Any]:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "created_at": k.created_at.isoformat(),
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
    }


def _serialize_workspace(w: Workspace) -> dict[str, Any]:
    return {
        "id": w.id,
        "name": w.name,
        "created_at": w.created_at.isoformat(),
        # Phase 11B.1: workspace-level monthly spend cap. NULL = no cap.
        "budget_usd_monthly": (
            float(w.budget_usd_monthly)
            if w.budget_usd_monthly is not None
            else None
        ),
        # Phase 17.7: billing surface so /account can render the right
        # CTA. has_stripe_customer is true iff the workspace has ever
        # been through Checkout (the portal endpoint 400s otherwise).
        "plan_tier": w.plan_tier,
        "free_credits_remaining_usd": float(w.free_credits_remaining_usd or 0),
        "has_stripe_customer": bool(w.stripe_customer_id),
        # Phase 21.9: when true, Polaris's widget-incident-response
        # scan auto-applies suggested_fix to the bot's system_prompt
        # without waiting for operator review.
        "polaris_auto_apply_widget_fixes": bool(
            getattr(w, "polaris_auto_apply_widget_fixes", False)
        ),
        # Phase 26.1: operator-claimed consumer-chat URL handle.
        # NULL until the operator claims one via
        # POST /workspaces/me/vendor-slug.
        "vendor_slug": getattr(w, "vendor_slug", None),
    }


app = FastAPI(title="Lightsei Backend", version="0.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BodySizeLimitMiddleware)


@app.on_event("startup")
def on_startup() -> None:
    upgrade_to_head()
    # Phase 11B.1: re-assert the model_pricing table from the
    # pricing.PRICING source-of-truth dict so a release can update
    # prices without a manual UPDATE. Idempotent.
    from pricing import seed_model_pricing
    with session_scope() as s:
        try:
            seed_model_pricing(s)
        except Exception as e:
            # Don't crash startup on a pricing-sync failure — the
            # cost computation in cost.py reads from the dict directly,
            # so worst case the dashboard's pricing-table view shows
            # stale rows for one cycle.
            import logging
            logging.getLogger("lightsei.startup").warning(
                "model_pricing seed failed: %s", e
            )

    # Phase 12C.6.2: in-process runner for /agents/generate and
    # /teams/plan. Picks pending generation_jobs rows and runs them
    # off the request path so long Anthropic calls don't race the
    # edge timeout. See backend/jobs.py.
    import jobs
    jobs.start_runner()

    # Phase 14.3: periodic eval cron. Drops one eval_runs job per
    # workspace per LIGHTSEI_EVAL_INTERVAL_S (default 3600s); the
    # runner above picks them up and the judge-LLM verdict lands on
    # run_evaluations.
    import eval_runner
    eval_runner.start_eval_cron()

    # Phase 22.3: cron-trigger scheduler. Ticks every 60s, enqueues
    # a `scheduled_run` job for any cron trigger whose next_run_at has
    # passed (within a 24h grace window). 22.4 ships the handler that
    # actually dispatches the bot run.
    import scheduler
    scheduler.start_scheduler()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    import eval_runner
    import jobs
    import scheduler
    await scheduler.stop_scheduler()
    await eval_runner.stop_eval_cron()
    await jobs.stop_runner()


# Phase 11.7 demo trigger v6: full auto-approve chain — should land green ✅ in Slack with zero clicks.
@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + lightweight pool/connection telemetry.

    The pool counters come from SQLAlchemy's QueuePool (free, no IO). The
    pg_stat_activity probe is one short query against a system view —
    cheap enough to run on every /health hit and gives us a real
    server-side view of connection state. Both are here (rather than a
    separate /metrics) so the existing keepalive cron and Railway's
    health check graph it for free.
    """
    from db import engine as _engine

    pool = _engine.pool
    pool_stats: dict[str, Any] = {
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
    }

    db_stats: dict[str, Any] = {}
    try:
        with _engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
                      COUNT(*) FILTER (WHERE state = 'active')              AS active,
                      COUNT(*) FILTER (WHERE state = 'idle')                AS idle,
                      COUNT(*)                                              AS total
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    """
                )
            ).one()
            db_stats = {
                "idle_in_txn": int(row.idle_in_txn or 0),
                "active": int(row.active or 0),
                "idle": int(row.idle or 0),
                "total": int(row.total or 0),
            }
    except Exception as e:
        db_stats = {"error": f"{type(e).__name__}: {e}"}

    return {"status": "ok", "pool": pool_stats, "db": db_stats}


def _rate_limited_workspace_id(
    auth: AuthResult = Depends(get_authenticated),
) -> str:
    """Rate-limits the /events ingest path per-credential, then returns the
    workspace_id. Per-credential (not per-workspace) so a leaked or runaway
    api_key throttles itself without taking out the dashboard's session."""
    cred_id = auth.api_key.id if auth.api_key else (
        auth.session.id if auth.session else auth.workspace_id
    )
    limits.limit_events_per_credential(cred_id)
    return auth.workspace_id


def _record_run_behavior(
    session: Session, workspace_id: str, run_id: str, agent_name: str
) -> list[RunBehavioralViolation]:
    """Phase 15.3: evaluate layer-4 behavioral rules over a finished run's
    events and upsert any violations into run_behavioral_violations.

    Called once at run-end (not per event) so it stays off the ingest hot
    path. Upserts on the unique (run_id, rule) index so a re-fired
    run-end event refreshes rather than duplicates. Advisory in v1: this
    records + surfaces violations; it does not halt the run.
    """
    rows = session.execute(
        select(Event)
        .where(Event.run_id == run_id)
        .order_by(Event.timestamp, Event.id)
    ).scalars().all()
    events = [
        {"kind": e.kind, "agent_name": e.agent_name, "payload": e.payload}
        for e in rows
    ]
    violations = behavioral_rules.evaluate_behavior(events)
    if not violations:
        return []
    now = utcnow()
    written: list[RunBehavioralViolation] = []
    for v in violations:
        existing = session.execute(
            select(RunBehavioralViolation).where(
                RunBehavioralViolation.run_id == run_id,
                RunBehavioralViolation.rule == v.rule,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.severity = v.severity
            existing.reason = v.reason
            existing.details = v.details
            existing.created_at = now
            written.append(existing)
        else:
            row = RunBehavioralViolation(
                id=str(uuid.uuid4()),
                run_id=run_id,
                workspace_id=workspace_id,
                agent_name=agent_name,
                rule=v.rule,
                severity=v.severity,
                reason=v.reason,
                details=v.details,
                created_at=now,
            )
            session.add(row)
            written.append(row)
    session.flush()
    return written


@app.post("/events")
def post_event(
    event: EventIn,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(_rate_limited_workspace_id),
) -> dict[str, Any]:
    ts = event.timestamp or utcnow()
    ensure_agent(session, workspace_id, event.agent_name, ts)

    # Phase 8.2: evaluate every registered validator pre-emit so the
    # pipeline can short-circuit on a blocking-mode FAIL before the
    # event row is created. Advisory results from the same pass get
    # written as event_validations rows after the insert succeeds.
    outcomes = evaluate_validators(
        session, workspace_id, event.kind, event.payload
    )
    blockers = find_blocking_failures(outcomes)
    if blockers:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "event rejected by blocking validator",
                "violations": [
                    {
                        "validator": o.validator_name,
                        **v,
                    }
                    for o in blockers
                    for v in o.violations
                ],
            },
        )

    run = session.get(Run, event.run_id)
    if run is None:
        run = Run(
            id=event.run_id,
            workspace_id=workspace_id,
            agent_name=event.agent_name,
            started_at=ts,
            ended_at=None,
        )
        session.add(run)
    elif run.workspace_id != workspace_id:
        # Run id collision across workspaces — refuse rather than leak.
        raise HTTPException(status_code=409, detail="run id belongs to another workspace")

    if event.kind in ("run_ended", "run_completed", "run_failed"):
        run.ended_at = ts
        # Phase 22.4: mirror the run's terminal status back to the
        # trigger that fired it, so the dashboard list query
        # (/agents/{name}/triggers) renders the latest outcome without
        # a JOIN on runs. Map the event kind to a short status string;
        # treat plain `run_ended` (no explicit success / failure) as
        # `succeeded` so the operator's most-recent-fire pill is
        # green by default.
        if run.triggered_by_trigger_id is not None:
            from models import Trigger as _Trigger
            t = session.get(_Trigger, run.triggered_by_trigger_id)
            if t is not None:
                t.last_run_status = (
                    "failed" if event.kind == "run_failed" else "succeeded"
                )
                t.updated_at = ts

    # Phase 11B.1: incrementally roll up per-run cost. We need a flushed
    # run row before add_run_cost_from_event can session.get(Run, ...),
    # so flush here for the freshly-added case.
    #
    # Phase 12D follow-up: failed calls bill input tokens at almost every
    # provider (the model loaded the prompt before the failure / refusal),
    # so they're real money that produced no output. Run them through the
    # same cost path — the helper already handles output_tokens=0 cleanly.
    if event.kind in ("llm_call_completed", "llm_call_failed"):
        session.flush()
        add_run_cost_from_event(session, event.run_id, event.payload or {})

    row = Event(
        workspace_id=workspace_id,
        run_id=event.run_id,
        agent_name=event.agent_name,
        kind=event.kind,
        payload=event.payload,
        timestamp=ts,
    )
    session.add(row)
    session.flush()

    # Phase 7.3 audit trail: persist every outcome (advisory and
    # blocking-pass alike) so the dashboard's chips and the
    # /events/{id}/validations endpoint have data to render.
    write_validation_rows(session, row.id, outcomes)

    # Phase 9.4: figure out which symbolic notification triggers fired
    # and enqueue dispatch tasks. The plan-building reads channels from
    # the DB synchronously (cheap), then BackgroundTasks runs the
    # actual HTTP-out after the response is sent (slow). A misconfigured
    # channel or unreachable webhook never blocks ingestion.
    fired = notification_triggers.detect_triggers(row, outcomes)
    if fired:
        # Attach validation outcomes to the signal payload for the
        # validation.fail trigger so the formatter can render specific
        # rules — the event's own payload doesn't carry post-emit
        # validation results.
        signal_payload = dict(row.payload or {})
        if any(o.status == "fail" for o in outcomes):
            signal_payload["validations"] = [
                {
                    "validator": o.validator_name,
                    "status": o.status,
                    "violations": o.violations,
                }
                for o in outcomes
            ]
        plans = notification_triggers.build_dispatch_plans(
            session,
            event=row,
            workspace_id=workspace_id,
            fired_triggers=fired,
            dashboard_url_for=_dashboard_url_for,
            payload_for_signal=signal_payload,
        )
        for plan in plans:
            background_tasks.add_task(
                notification_triggers.dispatch_and_persist, plan,
            )

    # Phase 15.3: layer-4 behavioral rules. Evaluate the whole run once,
    # at run-end, so the per-event ingest path stays cheap. Advisory in
    # v1: record + surface, don't halt the run.
    if event.kind in ("run_ended", "run_completed", "run_failed"):
        _record_run_behavior(session, workspace_id, event.run_id, run.agent_name)

    return {"id": row.id, "status": "ok"}


@app.post("/policy/check")
def post_policy_check(
    req: PolicyCheckIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    if req.agent_name:
        ensure_agent(session, workspace_id, req.agent_name, utcnow())
    return policies.evaluate(
        session,
        workspace_id=workspace_id,
        agent_name=req.agent_name,
        action=req.action,
        payload=req.payload,
    )


@app.get("/runs")
def get_runs(
    limit: int = 50,
    trigger_id: Optional[str] = None,
    with_summary: bool = False,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List runs for the workspace, newest first.

    Phase 22.8 adds the `trigger_id` filter for the per-trigger
    history view + a left-outer-joined `trigger_name` in each row
    so the /runs page can render a "Triggered by: cron (name)"
    badge without a follow-up fetch. The badge for runs whose
    trigger has since been deleted falls back to the snapshotted
    `trigger_kind` with no name.

    Phase 30.4.a adds `with_summary=true`. The default response is
    unchanged (id / agent_name / started_at / ended_at / trigger_*),
    matching what the dashboard's web /runs page consumes via
    fetchRuns() + a follow-up per-run events fetch summarized
    client-side. With `with_summary=true`, the handler does ONE
    batched events lookup for the returned run set and inlines the
    summary fields (model / input_tokens / output_tokens /
    latency_ms / event_count / denied / denial) computed server-side
    using the same rules as dashboard/app/api.ts summarize(). This
    spares the iOS app from an N+1 round-trip per Runs-list refresh.
    """
    limit = max(1, min(limit, 500))
    q = (
        select(Run, Trigger.name)
        .outerjoin(Trigger, Run.triggered_by_trigger_id == Trigger.id)
        .where(Run.workspace_id == workspace_id)
        .order_by(desc(Run.started_at))
        .limit(limit)
    )
    if trigger_id is not None:
        q = q.where(Run.triggered_by_trigger_id == trigger_id)

    rows = session.execute(q).all()

    # Pre-aggregate events when summaries are requested. Mirrors
    # dashboard/app/api.ts summarize(): latest model wins; tokens +
    # latency sum across llm_call_completed events; first policy_denied
    # row wins for the denial payload; event_count is the total events
    # per run (NOT filtered to the two interesting kinds, so the field
    # matches the web's events.length semantics).
    summaries: dict[str, dict[str, Any]] = {}
    if with_summary and rows:
        run_ids = [r.id for r, _ in rows]
        for rid in run_ids:
            summaries[rid] = {
                "model": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0.0,
                "event_count": 0,
                "denial": None,
            }
        ev_rows = session.execute(
            select(Event)
            .where(
                Event.workspace_id == workspace_id,
                Event.run_id.in_(run_ids),
            )
            .order_by(Event.run_id, Event.timestamp)
        ).scalars().all()
        for ev in ev_rows:
            agg = summaries.get(ev.run_id)
            if agg is None:
                continue
            agg["event_count"] += 1
            if ev.kind == "llm_call_completed":
                p = ev.payload or {}
                if p.get("model"):
                    agg["model"] = p["model"]
                agg["input_tokens"] += int(p.get("input_tokens") or 0)
                agg["output_tokens"] += int(p.get("output_tokens") or 0)
                dur = p.get("duration_s")
                if isinstance(dur, (int, float)):
                    agg["latency_ms"] += float(dur) * 1000.0
            elif ev.kind == "policy_denied" and agg["denial"] is None:
                p = ev.payload or {}
                agg["denial"] = {
                    "policy": p.get("policy"),
                    "reason": p.get("reason"),
                    "cap_usd": p.get("cap_usd"),
                    "cost_so_far_usd": p.get("cost_so_far_usd"),
                    "action": p.get("action"),
                }

    out: list[dict[str, Any]] = []
    for r, trig_name in rows:
        row: dict[str, Any] = {
            "id": r.id,
            "agent_name": r.agent_name,
            "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "triggered_by_trigger_id": r.triggered_by_trigger_id,
            "trigger_kind": r.trigger_kind,
            "trigger_name": trig_name,
        }
        if with_summary:
            agg = summaries.get(r.id, {})
            row.update({
                "model": agg.get("model"),
                "input_tokens": agg.get("input_tokens", 0),
                "output_tokens": agg.get("output_tokens", 0),
                "latency_ms": int(round(agg.get("latency_ms", 0.0))),
                "event_count": agg.get("event_count", 0),
                "denied": agg.get("denial") is not None,
                "denial": agg.get("denial"),
            })
        out.append(row)
    return {"runs": out}


@app.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="run not found")
    rows = session.execute(
        select(Event)
        .where(Event.workspace_id == workspace_id, Event.run_id == run_id)
        .order_by(Event.timestamp, Event.id)
    ).scalars().all()
    return {
        "run": {
            "id": run.id,
            "agent_name": run.agent_name,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        },
        "events": [
            {
                "id": e.id,
                "run_id": e.run_id,
                "agent_name": e.agent_name,
                "kind": e.kind,
                "payload": e.payload or {},
                "timestamp": e.timestamp.isoformat(),
            }
            for e in rows
        ],
    }


@app.get("/runs/{run_id}/behavior")
def get_run_behavior(
    run_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 15.4: layer-4 behavioral-rule violations recorded for a run.

    Returns the violations (loop / runaway_tokens / escalating_permissions)
    plus a rollup the dashboard's behavior chip reads: worst severity in
    {none, warn, block}."""
    run = session.get(Run, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="run not found")
    rows = session.execute(
        select(RunBehavioralViolation)
        .where(
            RunBehavioralViolation.workspace_id == workspace_id,
            RunBehavioralViolation.run_id == run_id,
        )
        .order_by(RunBehavioralViolation.created_at)
    ).scalars().all()
    worst = "none"
    if any(r.severity == "block" for r in rows):
        worst = "block"
    elif rows:
        worst = "warn"
    return {
        "run_id": run_id,
        "worst_severity": worst,
        "violations": [
            {
                "rule": r.rule,
                "severity": r.severity,
                "reason": r.reason,
                "details": r.details or {},
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


def _serialize_agent(a: Agent) -> dict[str, Any]:
    return {
        "name": a.name,
        "daily_cost_cap_usd": a.daily_cost_cap_usd,
        "system_prompt": a.system_prompt,
        # Phase 12.1: per-agent provider + model pin. null = use whatever
        # the SDK's auto-patches reported on the latest llm_call_completed.
        "provider": a.provider,
        "model": a.model,
        # Per-agent tick interval (seconds). null = use the bot's env default.
        "tick_interval_s": a.tick_interval_s,
        "description": a.description,
        # Phase 16.1: trust-zone sensitivity ladder.
        "sensitivity_level": a.sensitivity_level,
        # Phase 16.2: per-agent capability allow-list (default-deny).
        # SDK gate in 16.3 refuses ops not on this list.
        "capabilities": list(a.capabilities or []),
        # Phase 16.4: opt-in for cross-zone dispatch. False = same-zone-only
        # dispatches; True = source can target agents in different zones
        # (but auto-approval rules still apply on top).
        "dispatches_cross_zone": bool(a.dispatches_cross_zone),
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


@app.get("/agents")
def list_agents(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    # `lightsei.*` is reserved for synthetic platform agents (e.g. the
    # `lightsei.system` row that absorbs cost from server-side generator
    # calls). They have Run rows for cost accounting but aren't user bots,
    # so keep them off the agents list.
    rows = session.execute(
        select(Agent)
        .where(Agent.workspace_id == workspace_id)
        .where(~Agent.name.like("lightsei.%"))
        .order_by(Agent.name)
    ).scalars().all()
    return {"agents": [_serialize_agent(a) for a in rows]}


@app.get("/agents/{agent_name}")
def get_agent(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _serialize_agent(a)


@app.patch("/agents/{agent_name}")
def patch_agent(
    agent_name: str,
    body: AgentPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=500, detail="agent ensure failed")
    # Only update fields the caller actually included. None means "clear".
    fields = body.model_fields_set
    if "daily_cost_cap_usd" in fields:
        a.daily_cost_cap_usd = body.daily_cost_cap_usd
    if "system_prompt" in fields:
        # blank/whitespace -> clear; otherwise store as-is
        if body.system_prompt and body.system_prompt.strip():
            a.system_prompt = body.system_prompt
        else:
            a.system_prompt = None
    if "provider" in fields:
        if body.provider is None or not body.provider.strip():
            a.provider = None
        else:
            normalized = body.provider.strip().lower()
            if normalized not in SUPPORTED_PROVIDERS:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown provider {body.provider!r}; expected one "
                        f"of {sorted(SUPPORTED_PROVIDERS)}"
                    ),
                )
            a.provider = normalized
    if "model" in fields:
        if body.model is None or not body.model.strip():
            a.model = None
        else:
            a.model = body.model.strip()
    if "tick_interval_s" in fields:
        if body.tick_interval_s is None:
            a.tick_interval_s = None
        else:
            v = int(body.tick_interval_s)
            if v < AGENT_TICK_INTERVAL_MIN_S or v > AGENT_TICK_INTERVAL_MAX_S:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"tick_interval_s must be between "
                        f"{AGENT_TICK_INTERVAL_MIN_S} and "
                        f"{AGENT_TICK_INTERVAL_MAX_S} seconds (got {v})"
                    ),
                )
            a.tick_interval_s = v
    if "description" in fields:
        if body.description is None or not body.description.strip():
            a.description = None
        else:
            a.description = body.description.strip()
    if "sensitivity_level" in fields:
        from models import is_valid_sensitivity_level
        if body.sensitivity_level is None:
            # null = no-op rather than 'reset to default'; the existing
            # value stays. Forces the user to pick a valid level
            # explicitly if they want to change it.
            pass
        elif not is_valid_sensitivity_level(body.sensitivity_level):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"sensitivity_level must be one of "
                    f"public / internal / sensitive / pii "
                    f"(got {body.sensitivity_level!r})"
                ),
            )
        else:
            a.sensitivity_level = body.sensitivity_level
    if "dispatches_cross_zone" in fields and body.dispatches_cross_zone is not None:
        a.dispatches_cross_zone = bool(body.dispatches_cross_zone)
    a.updated_at = now
    session.flush()
    return _serialize_agent(a)


# ---------- Phase 16.2: per-agent capability allow-list ---------- #


class AgentCapabilitiesPatchIn(BaseModel):
    """Body for PATCH /agents/{name}/capabilities.

    `capabilities` is the full new allow-list (replace, not patch).
    Tiny enough to send whole; avoids the add-one-remove-one merge
    semantics we'd otherwise need.
    """
    capabilities: list[str]


@app.patch("/agents/{agent_name}/capabilities")
def patch_agent_capabilities(
    agent_name: str,
    body: AgentCapabilitiesPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 16.2: replace an agent's capability allow-list.

    Validates against `backend/capabilities.py`'s vocabulary; 422s with
    the problems list on any invalid entry so the dashboard can render
    inline errors per-line. Dedupes before persisting so two-entry
    lists with the same capability twice become one.

    No SDK enforcement yet — that's Phase 16.3 (capability gate on
    outbound ops). This endpoint just makes the storage exist so 16.3
    has something to gate against.
    """
    import capabilities as _caps

    problems = _caps.validate_capability_list(body.capabilities)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={"problems": problems},
        )

    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    a.capabilities = _caps.normalize_capability_list(body.capabilities)
    a.updated_at = utcnow()
    session.flush()
    return _serialize_agent(a)


@app.delete("/agents/{agent_name}")
def delete_agent(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    """Remove an agent row.

    Historical events / runs / commands are NOT deleted — those keep
    their string `agent_name` reference and stay visible on /runs and
    /dispatch as audit trail. The `agents` row going away just means
    the bot disappears from /agents and the constellation, future
    `ensure_agent` calls won't re-create it (well, they will if a new
    bot starts emitting under the same name — that's fine).

    GitHub agent paths and any auto-approval rules referencing this
    agent are left in place; the user can clean those up separately
    if they really want a full purge.
    """
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    session.delete(a)
    session.flush()
    return {"status": "ok"}


# ---------- Phase 22.2: triggers (cron + webhook) ---------- #


class TriggerCreateIn(BaseModel):
    """Body for POST /agents/{agent_name}/triggers.

    `kind` is 'cron' or 'webhook'. For cron, exactly one of `schedule`
    (raw 5-field expression) or `preset` (friendly name from
    `triggers.known_presets()`) must be set. For webhook, both are
    ignored: a fresh token is minted and the plaintext returned once
    in the response.
    """

    kind: str
    name: str
    schedule: Optional[str] = None
    preset: Optional[str] = None


class TriggerPatchIn(BaseModel):
    """Body for PATCH /triggers/{trigger_id}.

    All fields optional; only those set are applied. `schedule` is
    only meaningful for cron triggers; trying to set it on a webhook
    trigger 422s.
    """

    enabled: Optional[bool] = None
    name: Optional[str] = None
    schedule: Optional[str] = None


def _serialize_trigger(t: Trigger) -> dict[str, Any]:
    """Trigger row → API shape. Never includes the webhook_token_hash:
    even the hash is sensitive (a brute-force target). The plaintext
    is only returned in the POST response and never again."""
    return {
        "id": t.id,
        "workspace_id": t.workspace_id,
        "agent_name": t.agent_name,
        "kind": t.kind,
        "schedule": t.schedule,
        "name": t.name,
        "enabled": t.enabled,
        "next_run_at": (
            t.next_run_at.isoformat() if t.next_run_at else None
        ),
        "last_run_at": (
            t.last_run_at.isoformat() if t.last_run_at else None
        ),
        "last_run_id": t.last_run_id,
        "last_run_status": t.last_run_status,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


@app.post("/agents/{agent_name}/triggers")
def create_agent_trigger(
    agent_name: str,
    body: TriggerCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 22.2: create a trigger on an agent.

    422 on unknown kind, malformed cron, conflicting schedule+preset,
    or schedule fields on a webhook kind. 404 if the agent doesn't
    exist in the caller's workspace. On success returns the new
    trigger row PLUS (for kind=webhook) the plaintext token under
    `webhook_token`. Plaintext is only returned this once; the
    operator must capture it from the response.
    """
    import triggers as _trigmod

    if not is_valid_trigger_kind(body.kind):
        raise HTTPException(
            status_code=422,
            detail=f"unknown trigger kind: {body.kind!r}",
        )
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(
            status_code=422, detail="trigger name is required",
        )

    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")

    now = utcnow()
    schedule: Optional[str] = None
    next_run_at: Optional[datetime] = None
    webhook_token_hash: Optional[str] = None
    plaintext_token: Optional[str] = None

    if body.kind == "cron":
        try:
            schedule = _trigmod.resolve_schedule(
                schedule=body.schedule, preset=body.preset,
            )
            next_run_at = _trigmod.compute_next_run_at(schedule, now)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:  # webhook
        if body.schedule or body.preset:
            raise HTTPException(
                status_code=422,
                detail=(
                    "webhook triggers don't take a schedule; "
                    "they fire on POST /triggers/{token}/fire"
                ),
            )
        plaintext_token, webhook_token_hash = _trigmod.mint_webhook_token()

    t = Trigger(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind=body.kind,
        schedule=schedule,
        webhook_token_hash=webhook_token_hash,
        name=name,
        enabled=True,
        next_run_at=next_run_at,
        created_at=now,
        updated_at=now,
    )
    session.add(t)
    session.flush()

    out = _serialize_trigger(t)
    if plaintext_token is not None:
        # Returned once; never again. Operator copies from the modal.
        out["webhook_token"] = plaintext_token
    return out


@app.get("/agents/{agent_name}/triggers")
def list_agent_triggers(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, list[dict[str, Any]]]:
    """Phase 22.2: list triggers for an agent.

    404 if the agent doesn't exist in the caller's workspace. Returns
    `{triggers: [...]}` ordered by created_at desc so the newest is
    rendered at the top of the dashboard panel.
    """
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")

    rows = session.execute(
        select(Trigger)
        .where(Trigger.workspace_id == workspace_id)
        .where(Trigger.agent_name == agent_name)
        .order_by(desc(Trigger.created_at))
    ).scalars().all()
    return {"triggers": [_serialize_trigger(t) for t in rows]}


@app.patch("/triggers/{trigger_id}")
def patch_trigger(
    trigger_id: str,
    body: TriggerPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 22.2: update enabled, name, or schedule on a trigger.

    404 if the trigger doesn't exist OR belongs to a different
    workspace (don't leak existence across tenants). 422 if `schedule`
    is set on a webhook-kind trigger or if the new cron is malformed.
    Schedule changes recompute next_run_at so the scheduler picks up
    the new cadence on its next tick.
    """
    import triggers as _trigmod

    t = session.get(Trigger, trigger_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="trigger not found")

    changed = False
    if body.enabled is not None:
        t.enabled = body.enabled
        changed = True
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(
                status_code=422, detail="trigger name cannot be empty",
            )
        t.name = new_name
        changed = True
    if body.schedule is not None:
        if t.kind != "cron":
            raise HTTPException(
                status_code=422,
                detail="only cron triggers have a schedule",
            )
        try:
            _trigmod.validate_cron(body.schedule)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        t.schedule = body.schedule.strip()
        t.next_run_at = _trigmod.compute_next_run_at(
            t.schedule, utcnow(),
        )
        changed = True

    if changed:
        t.updated_at = utcnow()
        session.flush()
    return _serialize_trigger(t)


@app.delete("/triggers/{trigger_id}")
def delete_trigger(
    trigger_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    """Phase 22.2: hard-delete a trigger.

    404 on unknown id or wrong-tenant access. Past runs that were
    triggered by this row stay (the 22.4 `runs.triggered_by_trigger_id`
    column will SET NULL on delete via its FK), preserving history
    visibility on /runs.
    """
    t = session.get(Trigger, trigger_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="trigger not found")
    session.delete(t)
    session.flush()
    return {"status": "ok"}


class TriggerSchedulePreviewIn(BaseModel):
    """Body for POST /triggers/preview-schedule."""

    schedule: str
    count: int = 3


@app.post("/triggers/preview-schedule")
def preview_schedule(
    body: TriggerSchedulePreviewIn,
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 22.7: return the next N fire times for a cron expression.

    Lets the dashboard's cron picker render "next fires at…" without
    pulling croniter into the Next.js bundle. Authed (workspace_id
    enforced) but doesn't touch the DB — pure compute.

    422 on a malformed cron. `count` is clamped to [1, 10] so a
    typo can't generate megabytes of timestamps.
    """
    import triggers as _trigmod

    n = max(1, min(int(body.count or 3), 10))
    try:
        _trigmod.validate_cron(body.schedule)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    now = utcnow()
    out: list[str] = []
    after = now
    for _ in range(n):
        nxt = _trigmod.compute_next_run_at(body.schedule, after)
        out.append(nxt.isoformat())
        after = nxt
    return {"next_runs": out}


# Per-token rate limit on the public webhook endpoint. 60/minute is
# generous for honest automation (Zapier, cron-as-a-service) and
# tight enough that a misbehaving caller can't drown the worker queue.
_WEBHOOK_FIRE_LIMIT_PER_MIN = 60


@app.post("/triggers/{token}/fire")
async def fire_webhook_trigger(
    token: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 22.6: public endpoint that fires a webhook trigger.

    Token in the URL IS the auth (GitHub-shaped). Body is forwarded
    to the bot as `lightsei.trigger.webhook_payload`; JSON bodies are
    parsed, non-JSON bodies are wrapped as `{raw: <text>}`. Body cap
    comes from the global middleware (LIGHTSEI_MAX_BODY_BYTES).

    Returns immediately with `{run_id, status: "queued"}`; the bot
    runs out-of-band via the existing scheduled_run handler + SDK
    polling. Returns 404 for unknown tokens, tokens that don't belong
    to a webhook trigger, or disabled triggers — same response for
    all three so we don't leak whether the token shape is valid.
    """
    import json as _json

    import jobs as _jobs
    import triggers as _trigmod

    _digest = _trigmod.hash_webhook_token(token)

    # Per-token rate limit fires BEFORE the DB lookup so a flood of
    # invalid tokens still gets throttled (same plaintext token →
    # same key, but valid token holders also benefit from the cap).
    limits.rate_limit(
        f"trigger_fire:{_digest}",
        limit=_WEBHOOK_FIRE_LIMIT_PER_MIN,
    )

    trigger = session.execute(
        select(Trigger).where(Trigger.webhook_token_hash == _digest)
    ).scalars().first()
    # Same 404 for missing / non-webhook / disabled. Don't leak.
    if (
        trigger is None
        or trigger.kind != "webhook"
        or not trigger.enabled
    ):
        raise HTTPException(status_code=404, detail="trigger not found")

    body = await request.body()
    if body:
        try:
            webhook_payload: Any = _json.loads(body.decode("utf-8"))
            # Top-level non-dict (list, string, number) is valid JSON
            # but awkward to expose to the bot as a dict; wrap so
            # `lightsei.trigger.webhook_payload` is always a dict.
            if not isinstance(webhook_payload, dict):
                webhook_payload = {"value": webhook_payload}
        except (ValueError, UnicodeDecodeError):
            webhook_payload = {"raw": body.decode("utf-8", errors="replace")}
    else:
        webhook_payload = {}

    now = utcnow()
    run_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    _jobs.enqueue_job(
        session,
        job_id=job_id,
        workspace_id=trigger.workspace_id,
        kind="scheduled_run",
        request_payload={
            "trigger_id": trigger.id,
            "webhook_payload": webhook_payload,
            "run_id": run_id,
        },
    )
    trigger.last_run_at = now
    trigger.updated_at = now
    session.flush()

    return {
        "run_id": run_id,
        "status": "queued",
        "trigger_id": trigger.id,
    }


def _serialize_plan_event(
    row: Event, validations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": row.id,
        "run_id": row.run_id,
        "agent_name": row.agent_name,
        "timestamp": row.timestamp.isoformat(),
        "payload": row.payload or {},
    }
    if validations is not None:
        base["validations"] = validations
    return base


def _validation_summaries_for_events(
    session: Session, event_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """Bulk-fetch validation summaries for a list of event ids.

    Returns `{event_id: [{validator, status, violation_count}]}`. Used by
    the list-plans endpoint to avoid an N+1 fetch when rendering
    sidebar chips. Each entry is the lite view: enough to render a
    PASS / FAIL / WARN chip but no actual violation details.
    """
    if not event_ids:
        return {}
    rows = session.execute(
        select(EventValidation)
        .where(EventValidation.event_id.in_(event_ids))
        .order_by(EventValidation.event_id, EventValidation.validator_name)
    ).scalars().all()
    out: dict[int, list[dict[str, Any]]] = {eid: [] for eid in event_ids}
    for v in rows:
        out[v.event_id].append({
            "validator": v.validator_name,
            "status": v.status,
            "violation_count": len(v.violations or []),
        })
    return out


def _validations_for_event(
    session: Session, event_id: int
) -> list[dict[str, Any]]:
    """Full validation rows for one event, including violation details.

    Used by `/latest-plan` (single plan, fine to be fat) and
    `/events/{id}/validations`. The list-plans endpoint uses
    `_validation_summaries_for_events` instead to keep responses lean.
    """
    rows = session.execute(
        select(EventValidation)
        .where(EventValidation.event_id == event_id)
        .order_by(EventValidation.validator_name)
    ).scalars().all()
    return [
        {
            "validator": v.validator_name,
            "status": v.status,
            "violations": v.violations or [],
        }
        for v in rows
    ]


@app.get("/agents/{agent_name}/latest-plan")
def get_agent_latest_plan(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Most recent `polaris.plan` event for the given agent, scoped
    to the calling workspace. Returns 404 when the agent has no plan
    events yet.

    Includes the full validation results for the plan: the dashboard
    selects this plan by default, so we ship the violation details
    inline to avoid a follow-up fetch on first render. The list
    endpoint trades that off for response size and only includes
    summary chips.
    """
    row = session.execute(
        select(Event)
        .where(
            Event.workspace_id == workspace_id,
            Event.agent_name == agent_name,
            Event.kind == "polaris.plan",
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no plan yet")
    return _serialize_plan_event(row, _validations_for_event(session, row.id))


@app.get("/agents/{agent_name}/plans")
def list_agent_plans(
    agent_name: str,
    limit: int = 20,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Recent `polaris.plan` events for the given agent (newest
    first), capped at `limit` (1..100, default 20). Powers the
    dashboard's plan-history sidebar — returning full payloads
    means the sidebar and the detail pane share data without a
    second fetch.

    Each plan carries a `validations` array of summaries
    (`{validator, status, violation_count}`), enough to render a
    chip on each sidebar row without inflating the response with
    full violation lists. The dashboard fetches full violations via
    `/events/{event_id}/validations` when the user clicks a
    historical plan; the latest plan ships full violations on
    `/latest-plan`.

    Empty list (200) when the agent has no plan events yet —
    distinct from latest-plan's 404 because callers iterating
    history shouldn't have to special-case "agent never emitted."
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be 1..100")
    rows = session.execute(
        select(Event)
        .where(
            Event.workspace_id == workspace_id,
            Event.agent_name == agent_name,
            Event.kind == "polaris.plan",
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(limit)
    ).scalars().all()
    summaries = _validation_summaries_for_events(session, [r.id for r in rows])
    return {
        "plans": [
            _serialize_plan_event(r, summaries.get(r.id, []))
            for r in rows
        ]
    }


@app.get("/agents/{agent_name}/latest-cost-analysis")
def latest_cost_analysis(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 12D.2: most recent `polaris.cost_analysis` event for the
    given agent, scoped to the calling workspace. 404 when the agent
    has not emitted one yet (which is the common case for a fresh
    workspace — the home page treats 404 as "no insights to surface").
    """
    row = session.execute(
        select(Event)
        .where(
            Event.workspace_id == workspace_id,
            Event.agent_name == agent_name,
            Event.kind == "polaris.cost_analysis",
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no cost_analysis yet")
    return {
        "event_id": row.id,
        "timestamp": row.timestamp.isoformat(),
        "payload": row.payload or {},
    }


@app.get("/events/{event_id}/validations")
def get_event_validations(
    event_id: int,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Full validation results for one event.

    Workspace-scoped: 404 if the event doesn't exist or belongs to a
    different workspace. The check is two-step (event row exists,
    event.workspace_id matches) rather than one query so we never
    leak cross-workspace event existence via timing — both branches
    return the same 404 detail.
    """
    event = session.get(Event, event_id)
    if event is None or event.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="event not found")
    return {
        "event_id": event_id,
        "validations": _validations_for_event(session, event_id),
    }


@app.post("/workspaces")
def create_workspace(
    body: WorkspaceCreateIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Public signup. Creates a workspace plus its first API key, and returns
    the plaintext key once. The plaintext is never retrievable again.
    """
    now = utcnow()
    ws = Workspace(id=str(uuid.uuid4()), name=body.name, created_at=now)
    session.add(ws)
    session.flush()
    seed_default_validators(session, ws.id, now)

    plaintext = generate_key()
    api_key_row = ApiKey(
        id=str(uuid.uuid4()),
        workspace_id=ws.id,
        name=body.api_key_name,
        prefix=prefix_for_display(plaintext),
        hash=hash_token(plaintext),
        created_at=now,
    )
    session.add(api_key_row)
    session.flush()
    return {
        "workspace": _serialize_workspace(ws),
        "api_key": _serialize_api_key(api_key_row) | {"plaintext": plaintext},
    }


@app.get("/workspaces/me")
def get_me(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return _serialize_workspace(ws)


# ---------- Phase 23.3: multi-workspace CRUD ---------- #


class MyWorkspaceCreateIn(BaseModel):
    """Body for POST /me/workspaces. Distinct from the
    signup-time `WorkspaceCreateIn` (which carries an api_key_name)
    + from the legacy PATCH `WorkspacePatchIn` (which carries
    budget + auto-apply flags). Same module so the duplicate
    bites loudly: Python would otherwise let a second class with
    the same name silently shadow the first."""

    name: str


class MyWorkspacePatchIn(BaseModel):
    """Body for PATCH /me/workspaces/{id}. All fields optional;
    only fields provided are applied. See MyWorkspaceCreateIn for
    why this name is distinct from the legacy `WorkspacePatchIn`."""

    name: Optional[str] = None


def _require_session_user(auth):
    """Reject api-key auth from the workspace-CRUD surface. These
    endpoints mutate the calling session's active workspace and
    only make sense for the dashboard session user."""
    if auth.user is None or auth.session is None:
        raise HTTPException(
            status_code=401,
            detail="session required (api-key auth not accepted here)",
        )


def _check_owner(session: Session, user_id: str, workspace_id: str) -> WorkspaceMember:
    """Look up the membership row + verify role='owner'. 404 if no
    membership (don't leak existence to non-members), 403 if member
    but not owner."""
    row = session.get(WorkspaceMember, (user_id, workspace_id))
    if row is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if row.role != "owner":
        raise HTTPException(
            status_code=403, detail="only the workspace owner can do this",
        )
    return row


def _validate_workspace_name(name: str) -> str:
    """Normalize + length-check. 1-64 chars after strip; non-empty."""
    trimmed = (name or "").strip()
    if not trimmed:
        raise HTTPException(
            status_code=422, detail="workspace name is required",
        )
    if len(trimmed) > 64:
        raise HTTPException(
            status_code=422,
            detail="workspace name must be 64 characters or fewer",
        )
    return trimmed


def _serialize_membership(m: WorkspaceMember, ws: Workspace, is_active: bool) -> dict[str, Any]:
    """Per-membership row in the GET /me/workspaces list. Combines
    the workspace + the user's role + whether the calling session is
    currently active in this workspace."""
    return {
        "id": ws.id,
        "name": ws.name,
        "role": m.role,
        "joined_at": m.joined_at.isoformat(),
        "is_active": is_active,
        "plan_tier": ws.plan_tier,
        "created_at": ws.created_at.isoformat(),
        # Phase 26.1: surface the claimed consumer-chat handle so the
        # /workspace-settings page can render its slug-claim section
        # without a second round-trip. NULL until claimed.
        "vendor_slug": getattr(ws, "vendor_slug", None),
    }


@app.get("/me/workspaces")
def list_my_workspaces(
    auth=Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 23.3: list workspaces the calling user is a member of.

    Ordered by joined_at ascending so the user's primary (original)
    workspace stays at the top of the dropdown. Each row carries
    `is_active=true` for the workspace the calling session is
    currently pointed at — lets the dashboard render the checkmark
    without a follow-up fetch.
    """
    _require_session_user(auth)
    rows = session.execute(
        select(WorkspaceMember, Workspace)
        .join(Workspace, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == auth.user.id)
        .order_by(WorkspaceMember.joined_at)
    ).all()
    active = auth.session.active_workspace_id
    return {
        "workspaces": [
            _serialize_membership(m, w, is_active=(w.id == active))
            for m, w in rows
        ],
    }


@app.post("/me/workspaces")
def create_my_workspace(
    body: MyWorkspaceCreateIn,
    auth=Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 23.3: create a workspace + auto-switch to it.

    Inserts the calling user as `owner` in workspace_members + flips
    the session's active_workspace_id in one transaction. Next
    page load lands in the new workspace.

    No Stripe customer is created here; that happens lazily on first
    Checkout (existing flow from Phase 17.4). New workspaces start
    on `free` tier with $5 of credits.
    """
    _require_session_user(auth)
    name = _validate_workspace_name(body.name)
    now = utcnow()

    ws = Workspace(
        id=str(uuid.uuid4()),
        name=name,
        created_at=now,
        plan_tier="free",
        free_credits_remaining_usd=Decimal("5.00"),
    )
    session.add(ws)
    session.flush()
    seed_default_validators(session, ws.id, now)

    session.add(WorkspaceMember(
        user_id=auth.user.id, workspace_id=ws.id, role="owner",
    ))
    # Flip the session's active pointer so the next request lands
    # in the new workspace. Caller's session row is the one auth
    # resolved against; mutate it directly.
    auth.session.active_workspace_id = ws.id
    session.flush()

    # Re-fetch the membership row so its server_defaulted joined_at
    # is populated for the response.
    member = session.get(WorkspaceMember, (auth.user.id, ws.id))
    return _serialize_membership(member, ws, is_active=True)


@app.post("/me/workspaces/{workspace_id}/switch")
def switch_my_workspace(
    workspace_id: str,
    auth=Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 23.3: set the calling session's active_workspace_id.

    404 on non-membership (don't leak existence). On success,
    returns the now-active workspace; the dashboard router.refresh()
    pulls fresh data for it.
    """
    _require_session_user(auth)
    member = session.get(WorkspaceMember, (auth.user.id, workspace_id))
    if member is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        # Inconsistent state (member row but no workspace); treat as 404.
        raise HTTPException(status_code=404, detail="workspace not found")

    auth.session.active_workspace_id = workspace_id
    session.flush()
    return _serialize_membership(member, ws, is_active=True)


@app.patch("/me/workspaces/{workspace_id}")
def patch_my_workspace(
    workspace_id: str,
    body: MyWorkspacePatchIn,
    auth=Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 23.3: rename a workspace. Owner-only.

    Members (Phase 23B) can read the workspace name via the list
    endpoint but can't change it. Today only owners exist in
    workspace_members rows; the gate ships now so 23B doesn't
    re-litigate.
    """
    _require_session_user(auth)
    member = _check_owner(session, auth.user.id, workspace_id)
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    changed = False
    if body.name is not None:
        new_name = _validate_workspace_name(body.name)
        if new_name != ws.name:
            ws.name = new_name
            changed = True
    if changed:
        ws.updated_at = utcnow()
        session.flush()

    is_active = auth.session.active_workspace_id == workspace_id
    return _serialize_membership(member, ws, is_active=is_active)


@app.delete("/me/workspaces/{workspace_id}")
def delete_my_workspace(
    workspace_id: str,
    auth=Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 23.3: hard-delete a workspace. Owner-only.

    Refuses if it's the user's only workspace (would leave them
    orphaned with no active pointer + no fallback). If the deleted
    workspace was the session's active one, auto-switch to whichever
    other workspace the user joined earliest — saves the operator
    from a bounce through the picker page (23.6) for the common
    "delete a side project" flow.

    Cascade via existing FKs handles every workspace-scoped table
    (agents, runs, events, triggers, etc.).
    """
    _require_session_user(auth)
    _check_owner(session, auth.user.id, workspace_id)
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    # Refuse-last check: count member rows for this user.
    other_memberships = session.execute(
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == auth.user.id)
        .where(WorkspaceMember.workspace_id != workspace_id)
        .order_by(WorkspaceMember.joined_at)
    ).scalars().all()
    if not other_memberships:
        raise HTTPException(
            status_code=422,
            detail=(
                "can't delete your last workspace; create another one "
                "first or contact support to delete the account"
            ),
        )

    # Auto-switch if the deleted workspace is the active one. Pick
    # the user's oldest other membership as the new active workspace.
    switched_to: Optional[str] = None
    if auth.session.active_workspace_id == workspace_id:
        auth.session.active_workspace_id = other_memberships[0].workspace_id
        switched_to = other_memberships[0].workspace_id

    session.delete(ws)  # cascades agents / runs / events / etc.
    session.flush()
    return {
        "deleted": True,
        "workspace_id": workspace_id,
        "switched_to": switched_to,
    }


@app.get("/workspaces/me/cost")
def get_workspace_cost(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11B.1: month-to-date workspace spend + projected EOM +
    per-agent and per-model breakdown + budget bar state.

    Reads `runs.cost_usd` directly for the rollup so the dashboard can
    poll this every 30s without re-summing events × pricing per call.
    """
    return workspace_cost_mtd(session, workspace_id)


@app.post("/workspaces/me/feeder/digest")
def run_feeder_digest(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Generate a weekly business digest on demand.

    The feeder normally enqueues this on a weekly cadence (it rides the
    scheduler loop), which is what makes the AI Business Team proactive.
    This endpoint is the manual pull: the owner clicks "generate now" and
    gets a fresh `bi.summarize` queued immediately, bypassing the dedup
    window (force=True). The BI assistant picks it up on its next poll and
    writes the summary.

    Returns the new command id + the rolled-up digest data so the caller
    can show "here's what we're summarizing" without a second fetch. If
    the BI assistant isn't deployed there's nothing to claim the command,
    so we report that plainly instead of silently queueing into the void.
    """
    import feeder

    now = utcnow()
    has_bi = session.execute(
        text("SELECT 1 FROM agents WHERE workspace_id = :ws AND name = :name"),
        {"ws": workspace_id, "name": feeder.DIGEST_AGENT},
    ).first() is not None

    cmd_id = feeder.enqueue_digest_for_workspace(
        session, workspace_id, now, force=True
    )
    session.commit()

    return {
        "status": "queued",
        "command_id": cmd_id,
        "bi_assistant_deployed": has_bi,
        "note": (
            None if has_bi
            else "The Business Intelligence assistant is not deployed yet, "
            "so this digest will sit pending until it is."
        ),
    }


@app.get("/workspaces/me/feeder/digest/status")
def get_feeder_digest_status(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Most recent feeder-sourced digest for this workspace.

    Lets the dashboard render "last digest: 2 days ago, completed" and
    decide whether the "generate now" button should warn about the dedup
    window. Returns nulls when the feeder has never run here.
    """
    import feeder

    row = session.execute(
        text(
            """
            SELECT id, status, created_at, completed_at
              FROM commands
             WHERE workspace_id = :ws
               AND agent_name = :agent
               AND kind = :kind
               AND payload ->> 'source' = :source
             ORDER BY created_at DESC
             LIMIT 1
            """
        ),
        {
            "ws": workspace_id,
            "agent": feeder.DIGEST_AGENT,
            "kind": feeder.DIGEST_KIND,
            "source": feeder.DIGEST_SOURCE,
        },
    ).mappings().first()

    # The most recent written summary the BI assistant produced (from any
    # bi.summarize, feeder-sourced or not). This is the human-readable
    # output the dashboard card shows; the command row above is just the
    # request/status. Surfacing both in one response keeps the card to a
    # single fetch.
    summary_row = session.execute(
        text(
            """
            SELECT payload, timestamp
              FROM events
             WHERE workspace_id = :ws
               AND agent_name = :agent
               AND kind = 'bi.summary'
             ORDER BY timestamp DESC
             LIMIT 1
            """
        ),
        {"ws": workspace_id, "agent": feeder.DIGEST_AGENT},
    ).mappings().first()

    latest_summary = None
    if summary_row is not None:
        payload = summary_row["payload"] or {}
        latest_summary = {
            "text": payload.get("summary"),
            "kind": payload.get("kind"),
            "produced_at": summary_row["timestamp"].isoformat(),
        }

    if row is None:
        return {
            "last_digest": None,
            "latest_summary": latest_summary,
            "period_days": feeder.PERIOD_DAYS,
        }

    return {
        "last_digest": {
            "command_id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
            "completed_at": (
                row["completed_at"].isoformat() if row["completed_at"] else None
            ),
        },
        "latest_summary": latest_summary,
        "period_days": feeder.PERIOD_DAYS,
    }


@app.get("/workspaces/me/onboarding")
def get_onboarding(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """The onboarding wizard's data: the industry + goal catalog, plus this
    workspace's saved profile (null if onboarding isn't done yet).

    The dashboard uses `profile is null` to decide whether to show the
    welcome wizard.
    """
    import onboarding

    return {
        "catalog": onboarding.catalog(),
        "profile": onboarding.get_profile(session, workspace_id),
    }


class OnboardingSubmit(BaseModel):
    industry: Optional[str] = None
    goals: list[str] = []


@app.post("/workspaces/me/onboarding")
def submit_onboarding(
    body: OnboardingSubmit,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Provision a business's AI team from their answers.

    Maps {industry, goals} to a plan (which assistants to create, which
    feeders to turn on, which connectors they still need to connect),
    applies it, and returns the plan so the dashboard can show "here's
    your team, connect these next". Idempotent: re-submitting just
    re-applies (ensure_agent + feeder upsert + profile overwrite).
    """
    import onboarding

    plan = onboarding.build_provisioning_plan(body.industry, body.goals)
    onboarding.apply_provisioning_plan(session, workspace_id, plan, utcnow())
    session.commit()
    return {
        "status": "provisioned",
        "plan": plan,
        "profile": onboarding.get_profile(session, workspace_id),
    }


@app.get("/workspaces/me/feeders")
def list_feeders(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """The feeder catalog annotated with this workspace's on/off state.

    Every known feeder (weekly digest, spend alert) with its current
    enabled flag, defaulting to on where the owner has never toggled it.
    Powers the "Proactive feeders" settings surface.
    """
    import feeder

    return {"feeders": feeder.get_feeder_settings(session, workspace_id)}


class FeederToggle(BaseModel):
    enabled: bool


@app.patch("/workspaces/me/feeders/{feeder_kind}")
def set_feeder(
    feeder_kind: str,
    body: FeederToggle,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Turn one feeder on or off for this workspace.

    Rejects an unknown feeder_kind (404) so a typo can't silently create a
    dead settings row. Idempotent: setting the same value just bumps the
    timestamp.
    """
    import feeder

    known = {entry["kind"] for entry in feeder.FEEDER_CATALOG}
    if feeder_kind not in known:
        raise HTTPException(status_code=404, detail="unknown feeder")

    feeder.set_feeder_enabled(
        session, workspace_id, feeder_kind, body.enabled, utcnow()
    )
    session.commit()
    return {"feeders": feeder.get_feeder_settings(session, workspace_id)}


class FeederConfigBody(BaseModel):
    config: dict[str, Any]


def _feeder_catalog_entry(feeder_kind: str) -> dict[str, Any]:
    import feeder

    for entry in feeder.FEEDER_CATALOG:
        if entry["kind"] == feeder_kind:
            return entry
    raise HTTPException(status_code=404, detail="unknown feeder")


@app.patch("/workspaces/me/feeders/{feeder_kind}/config")
def set_feeder_config_endpoint(
    feeder_kind: str,
    body: FeederConfigBody,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Set a feeder's per-workspace target config.

    Only targetable feeders (those with a `target_connector`) accept config
    — a 400 otherwise so a caller can't stash arbitrary blobs on a feeder
    that ignores them. Setting config never flips the feeder on (a fresh
    row inherits the catalog default), so picking a target is safe before
    enabling.
    """
    import feeder

    entry = _feeder_catalog_entry(feeder_kind)
    if entry.get("target_connector") is None:
        raise HTTPException(
            status_code=400, detail="this feeder takes no target config"
        )

    feeder.set_feeder_config(
        session, workspace_id, feeder_kind, body.config, utcnow()
    )
    session.commit()
    return {"feeders": feeder.get_feeder_settings(session, workspace_id)}


@app.get("/workspaces/me/feeders/{feeder_kind}/targets")
def list_feeder_targets(
    feeder_kind: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enumerate the targets an owner can pick for a targetable feeder.

    For the Reputation feeder this walks the Google Business Profile
    connector (accounts -> locations) and returns each location as a
    pickable option. Goes through invoke_connector_tool as the feeder's
    target assistant, so the same gates apply. On any connector failure
    (not connected, not authorized) it returns an empty list + a reason
    rather than a 500, so the UI can prompt "connect the connector first".
    """
    import feeder

    entry = _feeder_catalog_entry(feeder_kind)
    connector_type = entry.get("target_connector")
    if connector_type is None:
        raise HTTPException(
            status_code=400, detail="this feeder takes no target"
        )

    # Currently only the google_business review feeder is targetable.
    source_agent = feeder.REPUTATION_AGENT
    try:
        accounts = (invoke_connector_tool(
            session, workspace_id=workspace_id, connector_type=connector_type,
            tool_name="list_accounts", payload={}, source_agent=source_agent,
        ) or {}).get("accounts") or []
    except HTTPException as exc:
        return {"targets": [], "available": False,
                "reason": _connector_unavailable_reason(exc)}

    targets: list[dict[str, Any]] = []
    for acc in accounts:
        acc_id = acc.get("id")
        if not acc_id:
            continue
        try:
            locations = (invoke_connector_tool(
                session, workspace_id=workspace_id,
                connector_type=connector_type, tool_name="list_locations",
                payload={"account_id": acc_id}, source_agent=source_agent,
            ) or {}).get("locations") or []
        except HTTPException:
            continue
        for loc in locations:
            loc_id = loc.get("id")
            if not loc_id:
                continue
            targets.append({
                "account_id": acc_id,
                "location_id": loc_id,
                "location_title": loc.get("title"),
                "account_name": acc.get("account_name"),
                "label": loc.get("title") or loc_id,
            })

    return {"targets": targets, "available": True, "reason": None}


def _connector_unavailable_reason(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("error") or detail.get("message") or "unavailable")
    return str(detail or "unavailable")


@app.get("/workspaces/me/zone-presets")
def get_zone_presets(
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 16.7: list the three trust-zone presets the team-from-README
    picker offers.

    Workspace-authed but workspace-independent — the presets are
    global. The auth check is here so an unauth caller can't enumerate
    the workspace's UI surfaces, but the response body doesn't depend
    on which workspace is asking.

    Each entry carries the dashboard-renderable metadata (label,
    summary, tradeoff) + the full `by_role` map so the picker can
    show a preview of what each role gets without a follow-up fetch.
    """
    import zone_presets as _zp
    return {"presets": _zp.list_presets()}


@app.get("/workspaces/me/cost/insights")
def get_cost_insights(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 12D.1: where your dollars went, what was wasted, and
    one-click fixes where they exist.

    Returns a homogeneous list of insight dicts (model-tier swaps that
    would save money, agents with low useful-rate, cache savings to
    date, failed-call cost, plan volatility for tick-interval tuning).
    Pure analytics over existing data — no LLM calls, no schema
    changes; safe to poll on every page load (the /cost/insights page
    refreshes every 30s the same way /cost does).
    """
    import cost_insights as _ci
    return _ci.all_insights(session, workspace_id)


@app.get("/workspaces/me/quality")
def get_workspace_quality(
    days: int = 7,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 14.4: workspace-wide quality rollup.

    Pure read over `run_evaluations` (filled by Phase 14.3's eval
    runner). Per-agent verdict counts + trend + workspace-wide
    aggregate + the top recent-bads list. Safe to poll on every
    /agents page load.
    """
    import quality_signal
    return quality_signal.workspace_quality(session, workspace_id, days=days)


@app.get("/workspaces/me/agents/{agent_name}/quality")
def get_agent_quality(
    agent_name: str,
    days: int = 7,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 14.4: per-agent quality summary.

    Powers /agents/{name}'s Quality section: verdict_counts,
    total_evaluations, trend vs the prior window of the same length,
    and the most-recent bad evaluations with their judge reasons so
    the user can see _why_ a bot is flagged without an extra fetch.
    """
    import quality_signal
    return quality_signal.agent_quality(
        session, workspace_id, agent_name, days=days,
    )


@app.get("/workspaces/me/pulse")
def get_workspace_pulse(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11B.2: drives the home-page status hero. Returns a small
    counts-and-times payload the hero can render in one read; polled
    every 5s so the hero copy stays current without a websocket.

    `status` is 'calm' when issues_count is 0, 'attention' otherwise.
    `issues` breaks down what wants the operator's eyes:
      pending_approvals  — Phase 11.2 commands stuck waiting on a click
      failed_validations — Phase 8 blocking-validator denials in the
                           last 24h that haven't been re-emitted clean
      budget_warnings    — workspace MTD ≥ 80% of budget_usd_monthly
                           (1 if true, 0 if cap unset or under threshold)
      stale_agents       — agents with heartbeats older than 5 min
                           (bots that died without an explicit stop)
    `last_event_at` powers the hero's pulsing constellation icon —
    when this value moves, the icon brightens for one polling cycle.
    """
    now = utcnow()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_5m = now - timedelta(minutes=5)

    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    pending_approvals = session.execute(
        text(
            """
            SELECT COUNT(*) AS n FROM commands
            WHERE workspace_id = :wsid
              AND approval_state = 'pending'
              AND expires_at > :now
            """
        ),
        {"wsid": workspace_id, "now": now},
    ).scalar_one()

    # Failed validations in the last 24h, EXCEPT ones whose validator
    # has been edited since the failure landed. Editing a rule on
    # /validators is the user's signal that they've addressed it; old
    # fails of that rule are stale (the new pattern may not even fire
    # on that input anymore) and shouldn't keep nagging the home pulse.
    failed_validations = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM event_validations ev
            JOIN events e ON e.id = ev.event_id
            LEFT JOIN validator_configs vc
              ON vc.workspace_id = e.workspace_id
              AND vc.event_kind = e.kind
              AND vc.validator_name = ev.validator_name
            WHERE e.workspace_id = :wsid
              AND ev.status = 'fail'
              AND ev.created_at >= :cutoff_24h
              AND (vc.updated_at IS NULL OR ev.created_at >= vc.updated_at)
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).scalar_one()

    # Budget warning: only meaningful when a cap is set. 80% threshold
    # matches the "warns at 80%, denies at 100%" copy in the Phase
    # 11B.1 cost panel.
    budget_warnings = 0
    if workspace.budget_usd_monthly is not None and float(
        workspace.budget_usd_monthly
    ) > 0:
        cost = workspace_cost_mtd(session, workspace_id)
        used_pct = cost.get("budget_used_pct") or 0.0
        if used_pct >= 80.0:
            budget_warnings = 1

    # Stale = recently-relevant agent that stopped heartbeating. The
    # earlier definition ("any agent that ever had a heartbeat but
    # hasn't beat in 5 min") flagged abandoned test bots from weeks
    # ago and quietly inflated the attention counter forever. Tighten:
    # require some signal of recent relevance (heartbeat or event in
    # the last 24h). An agent that was running last week and isn't now
    # is just stopped, not stale — drop it from the count.
    cutoff_24h_stale = now - timedelta(hours=24)
    stale_agents = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM (
                SELECT
                    a.name,
                    (SELECT MAX(ai.last_heartbeat_at)
                     FROM agent_instances ai
                     WHERE ai.workspace_id = :wsid
                       AND ai.agent_name = a.name) AS last_hb,
                    (SELECT MAX(e.timestamp)
                     FROM events e
                     WHERE e.workspace_id = :wsid
                       AND e.agent_name = a.name) AS last_event
                FROM agents a
                WHERE a.workspace_id = :wsid
            ) sub
            WHERE sub.last_hb IS NOT NULL
              AND sub.last_hb < :cutoff_5m
              AND (
                  sub.last_hb >= :cutoff_24h
                  OR (sub.last_event IS NOT NULL AND sub.last_event >= :cutoff_24h)
              )
            """
        ),
        {
            "wsid": workspace_id,
            "cutoff_5m": cutoff_5m,
            "cutoff_24h": cutoff_24h_stale,
        },
    ).scalar_one()

    issues_count = (
        int(pending_approvals or 0)
        + int(failed_validations or 0)
        + int(budget_warnings or 0)
        + int(stale_agents or 0)
    )

    agent_count = session.execute(
        select(func.count()).select_from(Agent).where(
            Agent.workspace_id == workspace_id
        )
    ).scalar_one()

    last_polaris_tick = session.execute(
        text(
            """
            SELECT MAX(timestamp) FROM events
            WHERE workspace_id = :wsid
              AND agent_name = 'polaris'
              AND kind = 'polaris.plan'
            """
        ),
        {"wsid": workspace_id},
    ).scalar_one()
    last_event = session.execute(
        text(
            """
            SELECT MAX(timestamp) FROM events
            WHERE workspace_id = :wsid
            """
        ),
        {"wsid": workspace_id},
    ).scalar_one()

    return {
        "status": "calm" if issues_count == 0 else "attention",
        "issues_count": issues_count,
        "issues": {
            "pending_approvals": int(pending_approvals or 0),
            "failed_validations": int(failed_validations or 0),
            "budget_warnings": int(budget_warnings or 0),
            "stale_agents": int(stale_agents or 0),
        },
        "workspace_name": workspace.name,
        "agent_count": int(agent_count or 0),
        "last_polaris_tick_at": (
            last_polaris_tick.isoformat() if last_polaris_tick else None
        ),
        "last_event_at": (
            last_event.isoformat() if last_event else None
        ),
        "as_of": now.isoformat(),
    }


@app.get("/workspaces/me/constellation")
def get_workspace_constellation(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11B.3: nodes + edges that drive the home-page constellation map.

    Returns:
      agents: one row per agent with role + status + 24h-window activity
              counters (runs, cost, dispatches in/out) + model + last-event
              timestamp. The home-page hero pulls its "last activity"
              animation trigger from the max(last_event_at) across this list.
      edges:  one row per (source, target) agent pair with at least one
              dispatched command in the last 24h. count_24h drives stroke
              opacity; last_at drives the brief edge-pulse animation when
              a fresh dispatch lands within the polling window.

    Polled by the dashboard every 5s (and paused when the tab is hidden).
    Filters out agents with no activity AND no agent_instances row — keeps
    "dead" agents that were never deployed off the canvas.
    """
    cutoff_24h = utcnow() - timedelta(hours=24)

    # Per-agent rollup. Single LEFT JOIN against agent_instances for
    # last-heartbeat-derived status, and aggregates against runs +
    # commands for the 24h activity counters.
    agent_rows = session.execute(
        text(
            """
            SELECT
                a.name,
                a.role,
                a.system_prompt,
                a.sensitivity_level,
                COALESCE(
                    (
                        SELECT MAX(ai.last_heartbeat_at)
                        FROM agent_instances ai
                        WHERE ai.workspace_id = :wsid
                          AND ai.agent_name = a.name
                    ),
                    NULL
                ) AS last_heartbeat_at,
                COALESCE(
                    (
                        SELECT COUNT(*) FROM runs r
                        WHERE r.workspace_id = :wsid
                          AND r.agent_name = a.name
                          AND r.started_at >= :cutoff_24h
                    ),
                    0
                ) AS runs_24h,
                COALESCE(
                    (
                        SELECT SUM(r.cost_usd) FROM runs r
                        WHERE r.workspace_id = :wsid
                          AND r.agent_name = a.name
                          AND r.started_at >= :cutoff_24h
                    ),
                    0
                ) AS cost_24h_usd,
                (
                    SELECT MAX(e.timestamp) FROM events e
                    WHERE e.workspace_id = :wsid
                      AND e.agent_name = a.name
                ) AS last_event_at,
                (
                    SELECT MAX(e.payload->>'model') FROM events e
                    WHERE e.workspace_id = :wsid
                      AND e.agent_name = a.name
                      AND e.kind = 'llm_call_completed'
                      AND e.timestamp >= :cutoff_24h
                ) AS recent_model
            FROM agents a
            WHERE a.workspace_id = :wsid
              AND a.name NOT LIKE 'lightsei.%'
            ORDER BY a.name
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).all()

    # Active-only filter so old test bots / dev agents don't crowd the
    # canvas. Keep an agent if any of:
    #   - role == "orchestrator" (polaris always renders, anchors center)
    #   - heartbeat within the last hour (currently running on a worker)
    #   - any event in the last 24h (recently active even without a heartbeat)
    # Anything else is treated as dormant and dropped from the response.
    now = utcnow()
    cutoff_1h = now - timedelta(hours=1)
    agents_out: list[dict[str, Any]] = []
    for r in agent_rows:
        last_hb = r.last_heartbeat_at
        last_event = r.last_event_at
        is_orchestrator = r.role == "orchestrator"
        recently_alive = last_hb is not None and last_hb >= cutoff_1h
        recently_active = last_event is not None and last_event >= cutoff_24h
        if not (is_orchestrator or recently_alive or recently_active):
            continue
        # Status mapping:
        #   active = heartbeat within last 60s
        #   stale  = heartbeat seen but > 60s ago
        #   stopped = no heartbeat ever (but had runs_24h, so they ran
        #            via a worker that didn't register an instance row)
        if last_hb is None:
            status = "stopped"
        elif (now - last_hb).total_seconds() <= 60:
            status = "active"
        else:
            status = "stale"
        agents_out.append({
            "name": r.name,
            "role": r.role,
            "model": r.recent_model,
            "status": status,
            "runs_24h": int(r.runs_24h or 0),
            "cost_24h_usd": float(round(r.cost_24h_usd or 0, 6)),
            "last_event_at": (
                r.last_event_at.isoformat() if r.last_event_at else None
            ),
            "last_heartbeat_at": (
                last_hb.isoformat() if last_hb else None
            ),
            # Phase 16.6: drives node coloring on the constellation map.
            "sensitivity_level": r.sensitivity_level or "internal",
        })

    # Dispatch edges: pairs of (source, target) with at least one
    # dispatched command in the last 24h. Phase 11.2 lit this up by
    # adding the source_agent column on commands; the constellation
    # map's bezier edges read straight from this query.
    edge_rows = session.execute(
        text(
            """
            SELECT
                source_agent AS src,
                agent_name   AS tgt,
                COUNT(*)     AS count_24h,
                MAX(created_at) AS last_at
            FROM commands
            WHERE workspace_id = :wsid
              AND source_agent IS NOT NULL
              AND created_at >= :cutoff_24h
            GROUP BY source_agent, agent_name
            ORDER BY count_24h DESC
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).all()

    # Filter edges to only those whose endpoints are both in the
    # rendered agent list — keeps the canvas free of dangling edges
    # to filtered-out dormant agents.
    rendered_names = {a["name"] for a in agents_out}
    edges_out = [
        {
            "from": e.src,
            "to": e.tgt,
            "count_24h": int(e.count_24h or 0),
            "last_at": e.last_at.isoformat() if e.last_at else None,
        }
        for e in edge_rows
        if e.src in rendered_names and e.tgt in rendered_names
    ]

    return {"agents": agents_out, "edges": edges_out}


@app.patch("/workspaces/me")
def patch_me(
    body: WorkspacePatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        ws.name = body.name
    if "budget_usd_monthly" in fields:
        # Phase 11B.1: explicit null clears the cap. A patch that omits the
        # field entirely leaves it untouched (model_fields_set captures the
        # difference).
        if body.budget_usd_monthly is None:
            ws.budget_usd_monthly = None
        else:
            if body.budget_usd_monthly < 0:
                raise HTTPException(
                    status_code=400,
                    detail="budget_usd_monthly must be >= 0",
                )
            ws.budget_usd_monthly = Decimal(format(body.budget_usd_monthly, ".2f"))
    if (
        "polaris_auto_apply_widget_fixes" in fields
        and body.polaris_auto_apply_widget_fixes is not None
    ):
        # Phase 21.9: opt in / out of auto-applying suggested fixes.
        ws.polaris_auto_apply_widget_fixes = bool(body.polaris_auto_apply_widget_fixes)
    session.flush()
    return _serialize_workspace(ws)


@app.post("/workspaces/me/vendor-slug")
def claim_vendor_slug(
    body: VendorSlugIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 26.1: claim a URL-safe vendor handle for this workspace.

    422 on invalid format (the regex + length check in
    `is_valid_vendor_slug`); 409 if another workspace already holds
    this slug. On success, persists the slug and returns the full
    serialized workspace so the dashboard can refresh its
    `vendor_slug` field in one round-trip.

    Re-claiming the SAME slug for the same workspace is a no-op
    (returns 200). Re-claiming a DIFFERENT slug overwrites the old
    one. v1 has no separate "release the slug" path; PATCH the
    workspace to a different one or wait for Phase 26B.
    """
    proposed = body.slug.strip()
    if not is_valid_vendor_slug(proposed):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_vendor_slug",
                "message": (
                    "vendor slug must be 3-32 chars, lowercase, "
                    "letters / digits / dashes, no leading or "
                    "trailing dash"
                ),
            },
        )

    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    # No-op when the workspace already holds the proposed slug.
    if ws.vendor_slug == proposed:
        return _serialize_workspace(ws)

    # Pre-check the unique constraint with a SELECT so we can
    # return 409 cleanly instead of a 500 from the IntegrityError.
    # Race window: a concurrent claim could still race past this
    # check; the IntegrityError catch below covers that.
    holder = session.execute(
        select(Workspace).where(Workspace.vendor_slug == proposed)
    ).scalar_one_or_none()
    if holder is not None and holder.id != workspace_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "vendor_slug_taken",
                "message": f"vendor slug {proposed!r} is already in use",
            },
        )

    ws.vendor_slug = proposed
    try:
        session.flush()
    except Exception:
        # IntegrityError from the unique constraint if a parallel
        # claim sneaked in between the select above and the flush.
        # Surface as 409 so the client gets the same error shape.
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "vendor_slug_taken",
                "message": f"vendor slug {proposed!r} is already in use",
            },
        )
    return _serialize_workspace(ws)


@app.get("/workspaces/me/api-keys")
def list_my_keys(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(ApiKey).where(ApiKey.workspace_id == workspace_id).order_by(ApiKey.created_at)
    ).scalars().all()
    return {"api_keys": [_serialize_api_key(k) for k in rows]}


@app.post("/workspaces/me/api-keys")
def create_my_key(
    body: ApiKeyCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    plaintext = generate_key()
    row = ApiKey(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=body.name,
        prefix=prefix_for_display(plaintext),
        hash=hash_token(plaintext),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return _serialize_api_key(row) | {"plaintext": plaintext}


@app.delete("/workspaces/me/api-keys/{key_id}")
def revoke_my_key(
    key_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = session.get(ApiKey, key_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise HTTPException(status_code=404, detail="api key not found")
    if auth.api_key is not None and row.id == auth.api_key.id:
        raise HTTPException(
            status_code=400, detail="cannot revoke the key used for this request"
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
    session.flush()
    return _serialize_api_key(row)


# ---------- /workspaces/me/secrets ---------- #

def _serialize_secret_meta(s: WorkspaceSecret) -> dict[str, Any]:
    """Metadata only — never exposes the encrypted blob or decrypted value."""
    return {
        "name": s.name,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _validate_secret_name(name: str) -> None:
    if not SECRET_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "secret name must start with a letter and contain only letters, "
                "digits, or underscores (max 64 chars)"
            ),
        )


def _require_secrets_available() -> None:
    if not secrets_crypto.is_available():
        raise HTTPException(
            status_code=503,
            detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured",
        )


@app.get("/workspaces/me/secrets")
def list_secrets(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Names + timestamps only. Values are never included in this endpoint —
    fetch each one individually via GET /workspaces/me/secrets/{name}."""
    rows = session.execute(
        select(WorkspaceSecret)
        .where(WorkspaceSecret.workspace_id == workspace_id)
        .order_by(WorkspaceSecret.name)
    ).scalars().all()
    return {"secrets": [_serialize_secret_meta(s) for s in rows]}


@app.get("/workspaces/me/secrets/{name}")
def get_secret(
    name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Returns the decrypted value. Anyone holding a workspace credential can
    read every secret in that workspace, by design — secrets are *not*
    a per-user permission boundary."""
    _validate_secret_name(name)
    _require_secrets_available()
    row = session.get(WorkspaceSecret, (workspace_id, name))
    if row is None:
        raise HTTPException(status_code=404, detail="secret not found")
    try:
        value = secrets_crypto.decrypt(row.encrypted_value)
    except Exception as e:
        # An encrypted blob that can't be decrypted is almost always the master
        # key changing without a rotation. Surface that clearly rather than
        # returning gibberish.
        raise HTTPException(
            status_code=500,
            detail=f"failed to decrypt secret (master key may have changed): {e}",
        )
    return {"name": row.name, "value": value}


@app.put("/workspaces/me/secrets/{name}")
def put_secret(
    name: str,
    body: SecretSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Idempotent create/update. The value is encrypted before being written
    to the row — plaintext never reaches disk."""
    _validate_secret_name(name)
    _require_secrets_available()
    now = utcnow()
    blob = secrets_crypto.encrypt(body.value)
    row = session.get(WorkspaceSecret, (workspace_id, name))
    if row is None:
        row = WorkspaceSecret(
            workspace_id=workspace_id,
            name=name,
            encrypted_value=blob,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.encrypted_value = blob
        row.updated_at = now
    session.flush()
    return _serialize_secret_meta(row)


@app.delete("/workspaces/me/secrets/{name}")
def delete_secret(
    name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    _validate_secret_name(name)
    row = session.get(WorkspaceSecret, (workspace_id, name))
    if row is None:
        raise HTTPException(status_code=404, detail="secret not found")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


# ---------- /workspaces/me/validators (Phase 7.3) ---------- #


def _validate_validator_path(event_kind: str, validator_name: str) -> None:
    if not EVENT_KIND_RE.match(event_kind):
        raise HTTPException(
            status_code=400,
            detail=(
                "event_kind must match [a-z][a-z0-9_.]{0,63} (lowercase, "
                "may contain dots and underscores)"
            ),
        )
    if not VALIDATOR_NAME_RE.match(validator_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "validator_name must match [a-z][a-z0-9_]{0,63} (lowercase, "
                "alphanumeric + underscore)"
            ),
        )
    if validator_name not in validators.REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"validator {validator_name!r} is not in the registry; "
                f"known: {sorted(validators.REGISTRY)}"
            ),
        )


def _serialize_validator_config(c: ValidatorConfig) -> dict[str, Any]:
    return {
        "event_kind": c.event_kind,
        "validator_name": c.validator_name,
        "config": c.config,
        "mode": c.mode,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


@app.get("/workspaces/me/validators")
def list_validators(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(ValidatorConfig)
        .where(ValidatorConfig.workspace_id == workspace_id)
        .order_by(ValidatorConfig.event_kind, ValidatorConfig.validator_name)
    ).scalars().all()
    return {"validators": [_serialize_validator_config(c) for c in rows]}


@app.put("/workspaces/me/validators/{event_kind}/{validator_name}")
def put_validator(
    event_kind: str,
    validator_name: str,
    body: ValidatorConfigSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    _validate_validator_path(event_kind, validator_name)
    if body.mode not in VALIDATOR_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"mode must be one of {list(VALIDATOR_MODES)}; got "
                f"{body.mode!r}"
            ),
        )
    now = utcnow()
    existing = session.get(
        ValidatorConfig, (workspace_id, event_kind, validator_name)
    )
    if existing is None:
        row = ValidatorConfig(
            workspace_id=workspace_id,
            event_kind=event_kind,
            validator_name=validator_name,
            config=body.config,
            mode=body.mode,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        existing.config = body.config
        existing.mode = body.mode
        existing.updated_at = now
        row = existing
    session.flush()
    return _serialize_validator_config(row)


@app.delete("/workspaces/me/validators/{event_kind}/{validator_name}")
def delete_validator(
    event_kind: str,
    validator_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    # Path-format validation only — we don't require the validator to be
    # in the registry to delete its row. If a registry entry was renamed
    # in code, the old config row is now orphaned and the operator needs
    # to be able to clean it up.
    if not EVENT_KIND_RE.match(event_kind) or not VALIDATOR_NAME_RE.match(validator_name):
        raise HTTPException(status_code=400, detail="malformed event_kind or validator_name")
    row = session.get(
        ValidatorConfig, (workspace_id, event_kind, validator_name)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="validator config not found")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


# Dashboard base URL the formatters bake into "View ↗" links inside
# notification messages. Configurable so a self-host can point at its
# own dashboard. Default matches prod.
DASHBOARD_BASE_URL = os.environ.get(
    "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
).rstrip("/")


def _dashboard_url_for(trigger: str, agent_name: str, run_id: Optional[str] = None) -> str:
    """Build the deep link a notification's "View" action points at.

    polaris.plan / validation.fail send users to the Polaris page;
    run_failed sends them to the run detail. The dashboard handles
    auth + workspace selection, so we don't pass a workspace id here.
    """
    if trigger == "run_failed" and run_id:
        return f"{DASHBOARD_BASE_URL}/runs/{run_id}"
    if agent_name == "polaris" or trigger in ("polaris.plan", "validation.fail"):
        return f"{DASHBOARD_BASE_URL}/polaris"
    return f"{DASHBOARD_BASE_URL}/agents/{agent_name}"


# ---------- /workspaces/me/notifications (Phase 9.1) ---------- #
#
# Channels are workspace-scoped, unique-by-name, with a list of symbolic
# triggers (`polaris.plan`, `validation.fail`, `run_failed`). The actual
# dispatcher (HTTP-out + per-platform formatter) lands in 9.2; the
# `/test` endpoint here writes a 'skipped' delivery row in the meantime
# so the API surface is settled before 9.2 fills it in.

NOTIFICATION_CHANNEL_TYPES = ("slack", "discord", "teams", "mattermost", "webhook")
NOTIFICATION_TRIGGERS = ("polaris.plan", "validation.fail", "run_failed")
NOTIFICATION_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_\- ]{0,63}$")


class NotificationChannelCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str
    target_url: str = Field(min_length=1, max_length=2048)
    triggers: list[str] = Field(default_factory=list)
    secret_token: Optional[str] = Field(default=None, max_length=512)
    is_active: bool = True


class NotificationChannelPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    target_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    triggers: Optional[list[str]] = None
    secret_token: Optional[str] = Field(default=None, max_length=512)
    is_active: Optional[bool] = None


def _validate_channel_input(name: str, type_: str, triggers: list[str]) -> None:
    if not NOTIFICATION_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "name must match [A-Za-z][A-Za-z0-9_\\- ]{0,63} (start with "
                "a letter; alphanumeric, underscore, hyphen, space)"
            ),
        )
    if type_ not in NOTIFICATION_CHANNEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"type must be one of {list(NOTIFICATION_CHANNEL_TYPES)}; "
                f"got {type_!r}"
            ),
        )
    bad_triggers = [t for t in triggers if t not in NOTIFICATION_TRIGGERS]
    if bad_triggers:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown triggers {bad_triggers}; allowed values are "
                f"{list(NOTIFICATION_TRIGGERS)}"
            ),
        )


def _mask_url(url: str) -> str:
    """Mask a webhook URL for display.

    Keeps scheme + host so the user can recognize the platform; truncates
    the path so the secret token (which lives in the path for Slack/
    Discord/Teams/Mattermost) is never echoed back. Last 4 chars of the
    path are kept as a "yes, this is the URL I added" identity hint.
    """
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return "***"
    if not p.scheme or not p.netloc:
        return "***"
    path = p.path or ""
    if len(path) > 8:
        masked_path = f"{path[:4]}...{path[-4:]}"
    elif path:
        masked_path = "/***"
    else:
        masked_path = ""
    return f"{p.scheme}://{p.netloc}{masked_path}"


def _serialize_notification_channel(c: NotificationChannel) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "target_url_masked": _mask_url(c.target_url),
        "triggers": list(c.triggers or []),
        "has_secret_token": c.secret_token is not None,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


def _serialize_notification_delivery(d: NotificationDelivery) -> dict[str, Any]:
    return {
        "id": d.id,
        "channel_id": d.channel_id,
        "event_id": d.event_id,
        "trigger": d.trigger,
        "status": d.status,
        "response_summary": d.response_summary or {},
        "attempt_count": d.attempt_count,
        "sent_at": d.sent_at.isoformat(),
    }


@app.get("/workspaces/me/notifications")
def list_notification_channels(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(NotificationChannel)
        .where(NotificationChannel.workspace_id == workspace_id)
        .order_by(NotificationChannel.created_at)
    ).scalars().all()
    return {"channels": [_serialize_notification_channel(c) for c in rows]}


@app.post("/workspaces/me/notifications")
def create_notification_channel(
    body: NotificationChannelCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    _validate_channel_input(body.name, body.type, body.triggers)
    # Conflict check is a separate query rather than relying on the
    # UNIQUE constraint exception so the error message is cleaner and
    # we don't pollute the SQL session with a rolled-back insert.
    existing = session.execute(
        select(NotificationChannel)
        .where(
            NotificationChannel.workspace_id == workspace_id,
            NotificationChannel.name == body.name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a notification channel named {body.name!r} already exists",
        )
    now = utcnow()
    row = NotificationChannel(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=body.name,
        type=body.type,
        target_url=body.target_url,
        triggers=body.triggers,
        secret_token=body.secret_token,
        is_active=body.is_active,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return _serialize_notification_channel(row)


@app.get("/workspaces/me/notifications/{channel_id}")
def get_notification_channel(
    channel_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    return _serialize_notification_channel(row)


@app.patch("/workspaces/me/notifications/{channel_id}")
def patch_notification_channel(
    channel_id: str,
    body: NotificationChannelPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    fields = body.model_fields_set
    # Re-validate any changed fields. Type isn't patchable here — to
    # change platform, delete and recreate. Keeps the upsert path tight.
    if "name" in fields and body.name is not None:
        if not NOTIFICATION_NAME_RE.match(body.name):
            raise HTTPException(status_code=400, detail="malformed name")
        if body.name != row.name:
            conflict = session.execute(
                select(NotificationChannel)
                .where(
                    NotificationChannel.workspace_id == workspace_id,
                    NotificationChannel.name == body.name,
                )
            ).scalar_one_or_none()
            if conflict is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"channel name {body.name!r} already exists",
                )
            row.name = body.name
    if "triggers" in fields and body.triggers is not None:
        bad = [t for t in body.triggers if t not in NOTIFICATION_TRIGGERS]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"unknown triggers {bad}",
            )
        row.triggers = body.triggers
    if "target_url" in fields and body.target_url is not None:
        row.target_url = body.target_url
    if "secret_token" in fields:
        # Explicit None means "clear"; missing means "leave alone".
        row.secret_token = body.secret_token
    if "is_active" in fields and body.is_active is not None:
        row.is_active = body.is_active
    row.updated_at = utcnow()
    session.flush()
    return _serialize_notification_channel(row)


@app.delete("/workspaces/me/notifications/{channel_id}")
def delete_notification_channel(
    channel_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


@app.post("/workspaces/me/notifications/dispatch")
def dispatch_to_channel(
    body: NotificationDispatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11.4: dispatch arbitrary text to a notification channel
    by name. Hermes (the workspace's notifier bot) calls this as the
    last hop in chains like polaris → atlas → hermes — Atlas hands
    Hermes a tidy summary line, Hermes hands it to Slack/Discord/etc.

    Returns 200 with the delivery row regardless of dispatch outcome.
    The HTTP status of the underlying webhook lives in
    `delivery.response_summary.http_status` so the bot can decide
    whether to retry or surface failure as a `hermes.send_failed`
    event.
    """
    channel = session.execute(
        select(NotificationChannel).where(
            NotificationChannel.workspace_id == workspace_id,
            NotificationChannel.name == body.channel_name,
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=404,
            detail=f"channel {body.channel_name!r} not found",
        )

    signal = notifications.Signal(
        trigger="hermes.post",
        agent_name="hermes",
        dashboard_url=f"{DASHBOARD_BASE_URL}/notifications",
        timestamp=utcnow(),
        payload={"text": body.text, "severity": body.severity},
        workspace_id=workspace_id,
    )
    result = notifications.dispatch(
        channel_type=channel.type,
        target_url=channel.target_url,
        signal=signal,
        secret_token=channel.secret_token,
    )

    delivery = NotificationDelivery(
        channel_id=channel.id,
        event_id=None,
        trigger="hermes.post",
        status=result.status,
        response_summary=result.response_summary,
        attempt_count=result.attempt_count,
        sent_at=utcnow(),
    )
    session.add(delivery)
    session.flush()
    return {"delivery": _serialize_notification_delivery(delivery)}


@app.post("/workspaces/me/notifications/{channel_id}/test")
def test_notification_channel(
    channel_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Fire a synthetic test message at this channel.

    Phase 9.2 swap: now does a real dispatch via the per-platform
    formatter + HTTP-out, records the result in `notification_
    deliveries`. Endpoint shape unchanged from Phase 9.1 so the
    dashboard's "send test" button doesn't need updating.

    Returns 200 with the delivery row regardless of dispatch outcome
    (sent vs failed). The HTTP status of the underlying webhook lives
    in `delivery.response_summary.http_status`. We deliberately don't
    surface failures as 5xx — a misconfigured channel is the user's
    problem to fix, not a server error to alert on.
    """
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")

    signal = notifications.Signal(
        trigger="test",
        agent_name=row.name,  # the test message references the channel by name
        dashboard_url=f"{DASHBOARD_BASE_URL}/account",
        timestamp=utcnow(),
        payload={},
        workspace_id=workspace_id,
    )
    result = notifications.dispatch(
        channel_type=row.type,
        target_url=row.target_url,
        signal=signal,
        secret_token=row.secret_token,
    )

    delivery = NotificationDelivery(
        channel_id=row.id,
        event_id=None,
        trigger="test",
        status=result.status,
        response_summary=result.response_summary,
        attempt_count=result.attempt_count,
        sent_at=utcnow(),
    )
    session.add(delivery)
    session.flush()
    return {"delivery": _serialize_notification_delivery(delivery)}


@app.get("/workspaces/me/notifications/{channel_id}/deliveries")
def list_notification_deliveries(
    channel_id: str,
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    rows = session.execute(
        select(NotificationDelivery)
        .where(NotificationDelivery.channel_id == channel_id)
        .order_by(NotificationDelivery.sent_at.desc(), NotificationDelivery.id.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "deliveries": [_serialize_notification_delivery(d) for d in rows]
    }


# ---------- /workspaces/me/github (Phase 10.1) ---------- #
#
# CRUD for the workspace's GitHub integration + the per-agent path
# mappings the webhook receiver (10.2) and the redeploy pipeline (10.3)
# read from. Helpers, regexes, and Pydantic models for these endpoints
# live in the input-models section earlier in the file.


@app.put("/workspaces/me/github")
def put_github_integration(
    body: GitHubIntegrationSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Register or update the workspace's GitHub integration.

    On first registration: validates the PAT against the GitHub API
    (so wrong tokens fail here, not at first webhook), generates a
    fresh webhook secret, encrypts both, returns the secret in
    plaintext exactly once.

    On update: re-validates the PAT (in case the user is rotating to
    a new token) but keeps the existing webhook secret so the user's
    GitHub-side webhook config keeps working without a re-paste.
    """
    if not GITHUB_OWNER_RE.match(body.repo_owner):
        raise HTTPException(status_code=400, detail="repo_owner has an invalid GitHub username shape")
    if not GITHUB_REPO_RE.match(body.repo_name):
        raise HTTPException(status_code=400, detail="repo_name has an invalid GitHub repo shape")
    if not GITHUB_BRANCH_RE.match(body.branch):
        raise HTTPException(status_code=400, detail="branch has an invalid format")
    if not secrets_crypto.is_available():
        raise HTTPException(
            status_code=503,
            detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured",
        )

    # Validate the PAT against GitHub. This is a network call but we
    # want it to be authoritative — a bad PAT registered here would
    # make every subsequent webhook + Polaris-fetch fail mysteriously.
    try:
        meta = github_api.validate_pat(
            repo_owner=body.repo_owner,
            repo_name=body.repo_name,
            pat=body.pat,
        )
    except github_api.GitHubAPIError as exc:
        if exc.kind == "transport":
            raise HTTPException(status_code=502, detail=exc.message) from exc
        raise HTTPException(status_code=400, detail=exc.message) from exc

    now = utcnow()
    existing = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()

    encrypted_pat = secrets_crypto.encrypt(body.pat)
    if existing is None:
        webhook_secret = _generate_webhook_secret()
        encrypted_webhook_secret = secrets_crypto.encrypt(webhook_secret)
        row = GitHubIntegration(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            repo_owner=body.repo_owner,
            repo_name=body.repo_name,
            branch=body.branch,
            encrypted_pat=encrypted_pat,
            encrypted_webhook_secret=encrypted_webhook_secret,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return {
            **_serialize_github_integration(
                row,
                pat_plaintext=body.pat,
                webhook_secret_plaintext=webhook_secret,
            ),
            "default_branch_from_github": meta.default_branch,
        }

    # Update path: rotate PAT, possibly change repo + branch, keep
    # existing webhook secret (so the user's GitHub-side config keeps
    # working without a re-paste). To rotate the secret, DELETE +
    # re-PUT.
    existing.repo_owner = body.repo_owner
    existing.repo_name = body.repo_name
    existing.branch = body.branch
    existing.encrypted_pat = encrypted_pat
    existing.updated_at = now
    session.flush()
    return {
        **_serialize_github_integration(existing, pat_plaintext=body.pat),
        "default_branch_from_github": meta.default_branch,
    }


@app.get("/workspaces/me/github")
def get_github_integration(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no GitHub integration registered")
    # Decrypt the PAT just to compute the display mask (first 4 + last 4).
    # Plaintext stays inside this function — the serializer always masks.
    try:
        pat_plaintext = secrets_crypto.decrypt(row.encrypted_pat)
    except Exception:
        pat_plaintext = None
    return _serialize_github_integration(row, pat_plaintext=pat_plaintext)


@app.delete("/workspaces/me/github")
def delete_github_integration(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    row = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no GitHub integration registered")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


@app.get("/workspaces/me/github/agents")
def list_github_agent_paths(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(GitHubAgentPath)
        .where(GitHubAgentPath.workspace_id == workspace_id)
        .order_by(GitHubAgentPath.agent_name)
    ).scalars().all()
    return {"agents": [_serialize_github_agent_path(r) for r in rows]}


@app.put("/workspaces/me/github/agents/{agent_name}")
def put_github_agent_path(
    agent_name: str,
    body: GitHubAgentPathSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    if not GITHUB_AGENT_NAME_RE.match(agent_name):
        raise HTTPException(
            status_code=400,
            detail="agent_name must match [A-Za-z][A-Za-z0-9_-]{0,63}",
        )
    _validate_github_path(body.path)
    now = utcnow()
    existing = session.get(
        GitHubAgentPath, (workspace_id, agent_name)
    )
    if existing is None:
        row = GitHubAgentPath(
            workspace_id=workspace_id,
            agent_name=agent_name,
            path=body.path,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        existing.path = body.path
        existing.updated_at = now
        row = existing
    session.flush()
    return _serialize_github_agent_path(row)


@app.delete("/workspaces/me/github/agents/{agent_name}")
def delete_github_agent_path(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    if not GITHUB_AGENT_NAME_RE.match(agent_name):
        raise HTTPException(status_code=400, detail="malformed agent_name")
    row = session.get(GitHubAgentPath, (workspace_id, agent_name))
    if row is None:
        raise HTTPException(status_code=404, detail="agent path not registered")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


# ---------- POST /webhooks/github (Phase 10.2) ---------- #
#
# Public endpoint — no Lightsei API key. GitHub posts here whenever a
# subscribed event fires on a registered repo. Authenticity is via
# HMAC-SHA256 of the raw body using the integration's webhook_secret
# (the one we revealed once in 10.1). We verify before doing anything
# state-changing.
#
# Order of operations:
#   1. Read raw body bytes (signature is over bytes, not the parsed dict).
#   2. Parse JSON to extract repo full_name — needed to find which
#      integration's secret to verify against.
#   3. Look up integration by (repo_owner, repo_name). 404 if none —
#      this also makes random scans noisy in logs without exposing
#      whether a workspace exists.
#   4. Decrypt the integration's webhook_secret and verify the
#      X-Hub-Signature-256 header. 401 on mismatch / missing.
#   5. Past this point, the request is authenticated. Branch into
#      event-type handlers. Anything we don't recognize 200s with a
#      `skipped` reason so GitHub stops retrying.
#
# We deliberately do NOT verify the signature before looking up the
# integration: we'd have nothing to verify against. The lookup itself
# is read-only and constant-time-ish (single PK-style index hit), so
# leaking "this repo is registered" via a 401-vs-404 differential is
# acceptable — repos on github.com are public knowledge anyway.


def _parse_repo_full_name(payload: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Extract (owner, name) from a GitHub webhook payload's `repository`
    block. Returns None if the field is missing or malformed — caller
    treats that as a malformed body."""
    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        return None
    owner, _, name = full_name.partition("/")
    if not owner or not name:
        return None
    return owner, name


def _verify_github_signature(
    *, raw_body: bytes, header_value: Optional[str], secret: str
) -> bool:
    """Constant-time-compare X-Hub-Signature-256 against HMAC of body.
    GitHub sends the value as `sha256=<hex>`. Anything else (missing
    header, wrong prefix, malformed hex) is a verification failure."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = header_value.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, provided)


def _push_touched_path(commits: list[dict[str, Any]], agent_path: str) -> bool:
    """True if any commit in the push touched a file under `agent_path`.

    A path "matches" a commit if any of `added/modified/removed` is
    either equal to `agent_path` (exact-file mapping) or starts with
    `agent_path + "/"` (directory mapping). We don't try to be clever
    about renames — GitHub reports renames as a remove + add pair, so
    they're already covered."""
    if not isinstance(commits, list):
        return False
    prefix = agent_path.rstrip("/") + "/"
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for field in ("added", "modified", "removed"):
            files = commit.get(field) or []
            if not isinstance(files, list):
                continue
            for f in files:
                if not isinstance(f, str):
                    continue
                if f == agent_path or f.startswith(prefix):
                    return True
    return False


def _collect_touched_paths(commits: list[dict[str, Any]]) -> list[str]:
    """Flatten added/modified/removed across all commits in a push.

    Polaris's evaluate_push handler matches these against POLARIS_PUSH_RULES
    glob patterns to decide which downstream agents to dispatch. Order is
    preserved, duplicates removed (a single file changed across multiple
    commits in one push only counts once for rule matching).
    """
    if not isinstance(commits, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for field in ("added", "modified", "removed"):
            files = commit.get(field) or []
            if not isinstance(files, list):
                continue
            for f in files:
                if isinstance(f, str) and f and f not in seen:
                    seen.add(f)
                    out.append(f)
    return out


def _retire_active_deployments_for_agent(
    session: Session,
    *,
    workspace_id: str,
    agent_name: str,
) -> int:
    """Flip `desired_state` to 'stopped' on every existing deployment for
    `(workspace_id, agent_name)` that's still queued / building / running.

    Called immediately before persisting a NEW deployment for the same
    agent, so the worker's claim loop + the running supervisor's
    desired-state poll cooperate to retire the old bundle and free the
    concurrency slot for the new one. Without this, every redeploy
    accumulates an orphan bot until MAX_CONCURRENT is hit and queues
    silently stall (the issue we worked around with manual `pkill`
    during the Phase 11.7 demo and the Phase 12.4 polaris dance).

    Returns the count of rows retired so the caller can log it.
    """
    rows = (
        session.execute(
            select(Deployment).where(
                Deployment.workspace_id == workspace_id,
                Deployment.agent_name == agent_name,
                Deployment.desired_state == "running",
                Deployment.status.in_(["queued", "building", "running"]),
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for d in rows:
        d.desired_state = "stopped"
        d.updated_at = now
    if rows:
        session.flush()
    return len(rows)


def _queue_github_redeploy(
    session: Session,
    *,
    integration: GitHubIntegration,
    agent_name: str,
    agent_path: str,
    commit_sha: str,
) -> Optional[str]:
    """Fetch the agent's directory at `commit_sha` via the GitHub
    Contents API, store the zip as a DeploymentBlob, and create a
    Deployment row with `source='github_push'` and
    `desired_state='running'` so the Phase 5 worker picks it up via
    its existing claim loop. Returns the new deployment id, or None if
    the fetch failed (logged but does not fail the webhook — GitHub
    would otherwise retry on transient errors and we'd dispatch the
    same push twice).

    Errors fall into two buckets:
      - GitHubAPIError: PAT lost access, repo deleted mid-flight, the
        agent path no longer exists, etc. We swallow and return None;
        the dashboard's Deployments panel will simply show no new row,
        and the user can re-trigger via `lightsei deploy` or by
        re-pushing.
      - Anything else (zip codec, DB IntegrityError): re-raised so it
        surfaces in the webhook response as a 500 — these are bugs in
        our code, not user error, and we want them noisy.
    """
    try:
        pat = secrets_crypto.decrypt(integration.encrypted_pat)
    except Exception:
        # Decryption failure is a server config issue (rotated key,
        # corrupted row). Don't try to deploy; log and let the user
        # re-register.
        return None

    try:
        zip_bytes = github_api.fetch_directory_zip(
            repo_owner=integration.repo_owner,
            repo_name=integration.repo_name,
            commit_sha=commit_sha,
            path=agent_path,
            pat=pat,
        )
    except github_api.GitHubAPIError:
        # Couldn't fetch the directory. The webhook stays a 200 because
        # GitHub already gave us all the data they have — they can't
        # help by retrying. The caller (webhook receiver) records this
        # as a non-deployment in the response.
        return None

    if len(zip_bytes) == 0:
        # Empty agent dir at this commit. Don't create a deploy with
        # nothing to run.
        return None

    now = utcnow()
    sha = hashlib.sha256(zip_bytes).hexdigest()

    blob = DeploymentBlob(
        id=str(uuid.uuid4()),
        workspace_id=integration.workspace_id,
        size_bytes=len(zip_bytes),
        sha256=sha,
        data=zip_bytes,
        created_at=now,
    )
    session.add(blob)
    session.flush()

    ensure_agent(session, integration.workspace_id, agent_name, now)
    _retire_active_deployments_for_agent(
        session,
        workspace_id=integration.workspace_id,
        agent_name=agent_name,
    )
    dep = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=integration.workspace_id,
        agent_name=agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=blob.id,
        source="github_push",
        source_commit_sha=commit_sha,
        created_at=now,
        updated_at=now,
    )
    session.add(dep)
    session.flush()
    return dep.id


# ---------- Phase 10B.2: GitHub OAuth connect + multi-repo ---------- #


def _serialize_github_connection(c: GithubConnection) -> dict[str, Any]:
    """Non-secret view of a connection. The token never leaves storage."""
    return {
        "id": c.id,
        "auth_kind": c.auth_kind,
        "github_login": c.github_login,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


def _serialize_github_repo(r: GithubRepo, *, webhook_secret: Optional[str] = None) -> dict[str, Any]:
    out = {
        "id": r.id,
        "repo_owner": r.repo_owner,
        "repo_name": r.repo_name,
        "branch": r.branch,
        "is_active": r.is_active,
        "created_at": r.created_at.isoformat(),
    }
    if webhook_secret is not None:
        out["webhook_secret"] = webhook_secret  # returned exactly once
    return out


@app.get("/workspaces/me/github/oauth/start")
def github_oauth_start(
    redirect_after: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 10B.2: begin the GitHub OAuth connect.

    Mints a CSRF state bound to the workspace (reusing the connector
    pending-state table with connector_type='github'; GitHub's web flow
    has no PKCE so code_verifier is empty), and returns the authorize URL
    the dashboard navigates the browser to.
    """
    if not github_oauth.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub OAuth is not configured on this backend. Set "
                "LIGHTSEI_GITHUB_CLIENT_ID + LIGHTSEI_GITHUB_CLIENT_SECRET."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="GitHub connect must be initiated by a logged-in user",
        )
    now = utcnow()
    state = github_oauth.new_state()
    session.add(ConnectorOAuthPendingState(
        state=state,
        workspace_id=auth.workspace_id,
        installed_by_user_id=auth.user.id,
        connector_type="github",
        code_verifier="",  # GitHub web flow has no PKCE
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + CONNECTOR_OAUTH_STATE_TTL,
    ))
    session.flush()
    return {
        "authorization_url": github_oauth.build_authorization_url(state=state),
        "state": state,
    }


@app.get("/github/oauth/callback")
def github_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Response:
    """Phase 10B.2: complete the GitHub OAuth connect.

    Validates + single-uses the state, exchanges the code for a token,
    upserts the workspace's `github_connections` row (one per workspace),
    and redirects back to the dashboard. Repos are added separately via
    POST /workspaces/me/github/repos.
    """
    dashboard_base = _dashboard_base_url()

    def _html_error(title: str, message: str) -> Response:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title} — Lightsei</title></head><body style='font-family:sans-serif;max-width:32rem;margin:4rem auto'>"
            f"<h2>{title}</h2><p>{message}</p>"
            f"<p><a href='{dashboard_base}/github'>Back to GitHub settings</a></p>"
            "</body></html>"
        )
        return Response(content=body, media_type="text/html", status_code=400)

    if error:
        return _html_error("GitHub connect cancelled", f"GitHub returned: {error}. You can try again.")
    if not code or not state:
        return _html_error("Invalid connect link", "The callback was missing required parameters. Start the connect again.")

    pending = session.get(ConnectorOAuthPendingState, state)
    if pending is None or pending.connector_type != "github" or pending.expires_at < utcnow():
        return _html_error("Connect link expired", "The link is no longer valid (expired or already used). Start a fresh connect.")

    workspace_id = pending.workspace_id
    redirect_after = pending.redirect_after
    session.delete(pending)  # single-use
    session.flush()

    try:
        token = github_oauth.exchange_code_for_token(code=code)
    except github_oauth.GitHubOAuthError as exc:
        return _html_error("Connect didn't complete", f"The token exchange with GitHub failed: {exc}. Try again.")

    # Best-effort: label the connection with the GitHub login. A failure
    # here doesn't block the connect (login stays null).
    login: Optional[str] = None
    try:
        import httpx as _httpx
        r = _httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        if r.status_code < 400:
            login = r.json().get("login")
    except Exception:
        pass

    now = utcnow()
    encrypted = secrets_crypto.encrypt(token)
    existing = session.execute(
        select(GithubConnection).where(GithubConnection.workspace_id == workspace_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.encrypted_token = encrypted
        existing.auth_kind = "oauth"
        existing.github_login = login or existing.github_login
        existing.updated_at = now
    else:
        session.add(GithubConnection(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            encrypted_token=encrypted,
            auth_kind="oauth",
            github_login=login,
            created_at=now,
            updated_at=now,
        ))
    session.flush()

    target = redirect_after or f"{dashboard_base}/github?connected=1"
    return RedirectResponse(target, status_code=303)


@app.get("/workspaces/me/github/connection")
def get_github_connection(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Connection state + the repos under it (no secrets)."""
    conn = session.execute(
        select(GithubConnection).where(GithubConnection.workspace_id == workspace_id)
    ).scalar_one_or_none()
    repos = session.execute(
        select(GithubRepo).where(GithubRepo.workspace_id == workspace_id)
        .order_by(GithubRepo.created_at)
    ).scalars().all()
    return {
        "connection": _serialize_github_connection(conn) if conn else None,
        "repos": [_serialize_github_repo(r) for r in repos],
    }


@app.post("/workspaces/me/github/repos")
def add_github_repo(
    body: GitHubRepoAddIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 10B.2: register a repo under the workspace's connection.

    Validates the repo is reachable with the connection's token, generates
    a fresh per-repo webhook secret (returned plaintext exactly once), and
    inserts a github_repos row. Idempotent on (workspace, owner, name):
    re-adding updates the branch + keeps the existing webhook secret.
    """
    if not GITHUB_OWNER_RE.match(body.repo_owner):
        raise HTTPException(status_code=400, detail="repo_owner has an invalid GitHub username shape")
    if not GITHUB_REPO_RE.match(body.repo_name):
        raise HTTPException(status_code=400, detail="repo_name has an invalid GitHub repo shape")
    if not GITHUB_BRANCH_RE.match(body.branch):
        raise HTTPException(status_code=400, detail="branch has an invalid format")
    if not secrets_crypto.is_available():
        raise HTTPException(status_code=503, detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured")

    conn = session.execute(
        select(GithubConnection).where(GithubConnection.workspace_id == workspace_id)
    ).scalar_one_or_none()
    if conn is None:
        raise HTTPException(status_code=400, detail="connect GitHub first (no token on this workspace)")

    token = secrets_crypto.decrypt(conn.encrypted_token)
    try:
        github_api.validate_pat(
            repo_owner=body.repo_owner, repo_name=body.repo_name, pat=token,
        )
    except github_api.GitHubAPIError as exc:
        if exc.kind == "transport":
            raise HTTPException(status_code=502, detail=exc.message) from exc
        raise HTTPException(status_code=400, detail=exc.message) from exc

    now = utcnow()
    existing = session.execute(
        select(GithubRepo).where(
            GithubRepo.workspace_id == workspace_id,
            GithubRepo.repo_owner == body.repo_owner,
            GithubRepo.repo_name == body.repo_name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.branch = body.branch
        existing.connection_id = conn.id
        existing.is_active = True
        existing.updated_at = now
        session.flush()
        return _serialize_github_repo(existing)

    webhook_secret = _generate_webhook_secret()
    row = GithubRepo(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        connection_id=conn.id,
        repo_owner=body.repo_owner,
        repo_name=body.repo_name,
        branch=body.branch,
        encrypted_webhook_secret=secrets_crypto.encrypt(webhook_secret),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return _serialize_github_repo(row, webhook_secret=webhook_secret)


@app.delete("/workspaces/me/github/repos/{repo_id}")
def remove_github_repo(
    repo_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.get(GithubRepo, repo_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="repo not found")
    session.delete(row)
    session.flush()
    return {"deleted": repo_id}


def _serialize_branch_target(t: GithubRepoBranchTarget) -> dict[str, Any]:
    return {
        "id": t.id,
        "repo_id": t.repo_id,
        "branch": t.branch,
        "agent_name": t.agent_name,
        "created_at": t.created_at.isoformat(),
    }


def _owned_github_repo(session: Session, repo_id: str, workspace_id: str) -> GithubRepo:
    repo = session.get(GithubRepo, repo_id)
    if repo is None or repo.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="repo not found")
    return repo


@app.get("/workspaces/me/github/repos/{repo_id}/branch-targets")
def list_github_branch_targets(
    repo_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    _owned_github_repo(session, repo_id, workspace_id)
    rows = session.execute(
        select(GithubRepoBranchTarget)
        .where(GithubRepoBranchTarget.repo_id == repo_id)
        .order_by(GithubRepoBranchTarget.branch, GithubRepoBranchTarget.agent_name)
    ).scalars().all()
    return {"branch_targets": [_serialize_branch_target(t) for t in rows]}


@app.post("/workspaces/me/github/repos/{repo_id}/branch-targets")
def add_github_branch_target(
    repo_id: str,
    body: GitHubBranchTargetAddIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 10B.4: map a branch -> agent for this repo. Once any target
    exists on a repo, the webhook routes per-branch (only mapped agents
    deploy, on their branch). Idempotent on (repo, branch, agent)."""
    if not GITHUB_BRANCH_RE.match(body.branch):
        raise HTTPException(status_code=400, detail="branch has an invalid format")
    repo = _owned_github_repo(session, repo_id, workspace_id)
    existing = session.execute(
        select(GithubRepoBranchTarget).where(
            GithubRepoBranchTarget.repo_id == repo_id,
            GithubRepoBranchTarget.branch == body.branch,
            GithubRepoBranchTarget.agent_name == body.agent_name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _serialize_branch_target(existing)
    now = utcnow()
    ensure_agent(session, workspace_id, body.agent_name, now)
    row = GithubRepoBranchTarget(
        id=str(uuid.uuid4()),
        repo_id=repo_id,
        workspace_id=repo.workspace_id,
        branch=body.branch,
        agent_name=body.agent_name,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return _serialize_branch_target(row)


@app.delete("/workspaces/me/github/repos/{repo_id}/branch-targets/{target_id}")
def remove_github_branch_target(
    repo_id: str,
    target_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    _owned_github_repo(session, repo_id, workspace_id)
    row = session.get(GithubRepoBranchTarget, target_id)
    if row is None or row.repo_id != repo_id or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="branch target not found")
    session.delete(row)
    session.flush()
    return {"deleted": target_id}


def _resolve_github_target(
    session: Session, *, owner: str, name: str, raw_body: bytes, sig_header: Optional[str]
):
    """Phase 10B.3b: resolve an incoming push to its registered target,
    authenticated by signature.

    Prefers the multi-repo `github_repos` model: of the rows for
    (owner, name) — there can be several across workspaces — the one
    whose webhook secret verifies the signature is the target (the token
    comes from its parent `github_connections`). Falls back to the legacy
    `github_integrations` row; the 0049 backfill copied secrets verbatim,
    so existing connections verify under either path, making the cutover
    non-breaking.

    Returns an integration-shaped object (the attributes the webhook
    handler + `_queue_github_redeploy` read: workspace_id, repo_owner,
    repo_name, branch, is_active, encrypted_pat, encrypted_webhook_secret).
    Raises 404 (no such repo registered anywhere) or 401 (bad signature).
    """
    saw_candidate = False
    repo_rows = session.execute(
        select(GithubRepo).where(
            GithubRepo.repo_owner == owner, GithubRepo.repo_name == name,
        )
    ).scalars().all()
    for r in repo_rows:
        saw_candidate = True
        try:
            secret = secrets_crypto.decrypt(r.encrypted_webhook_secret)
        except Exception:
            continue  # undecryptable secret on this row; try the others
        if _verify_github_signature(raw_body=raw_body, header_value=sig_header, secret=secret):
            conn = session.get(GithubConnection, r.connection_id)
            if conn is None:
                raise HTTPException(status_code=500, detail="github connection missing for repo")
            return types.SimpleNamespace(
                repo_id=r.id,
                workspace_id=r.workspace_id,
                repo_owner=r.repo_owner,
                repo_name=r.repo_name,
                branch=r.branch,
                is_active=r.is_active,
                encrypted_pat=conn.encrypted_token,
                encrypted_webhook_secret=r.encrypted_webhook_secret,
            )

    # Legacy fallback: the pre-10B single-row integration.
    legacy = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.repo_owner == owner, GitHubIntegration.repo_name == name,
        )
    ).scalar_one_or_none()
    if legacy is not None:
        saw_candidate = True
        try:
            secret = secrets_crypto.decrypt(legacy.encrypted_webhook_secret)
        except Exception:
            raise HTTPException(status_code=500, detail="integration secret unavailable")
        if _verify_github_signature(raw_body=raw_body, header_value=sig_header, secret=secret):
            return legacy

    if not saw_candidate:
        raise HTTPException(status_code=404, detail=f"no integration for {owner}/{name}")
    raise HTTPException(status_code=401, detail="invalid signature")


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="body is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")

    parsed = _parse_repo_full_name(payload)
    if parsed is None:
        raise HTTPException(
            status_code=400, detail="payload missing repository.full_name"
        )
    owner, name = parsed

    # Phase 10B.3b: resolve + authenticate against github_repos first
    # (multi-repo, OAuth or PAT via the connection), falling back to the
    # legacy github_integrations row. Signature-based selection is what
    # lets several workspaces watch the same repo with distinct secrets.
    sig_header = request.headers.get("x-hub-signature-256")
    integration = _resolve_github_target(
        session, owner=owner, name=name, raw_body=raw_body, sig_header=sig_header,
    )

    # ----- past this line the request is authenticated ----- #

    if not integration.is_active:
        # User disabled the integration but the GitHub webhook is
        # still pointed at us. Quietly accept so GitHub doesn't retry,
        # but do nothing.
        return {
            "status": "ok",
            "event": request.headers.get("x-github-event"),
            "skipped": "integration_inactive",
        }

    event_type = request.headers.get("x-github-event") or "unknown"

    if event_type == "ping":
        # GitHub fires `ping` immediately after webhook creation. The
        # zen quote in the body is meaningless to us; just acknowledge.
        return {"status": "ok", "event": "ping"}

    if event_type != "push":
        # Tag pushes (`create`), branch deletes (`delete`), PRs, etc.
        # Phase 10 only handles `push`. 200 with a hint so the user
        # can debug from the GitHub webhook log if they were expecting
        # a deploy.
        return {
            "status": "ok",
            "event": event_type,
            "skipped": "event_type_not_handled",
        }

    ref = payload.get("ref") or ""
    push_branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    # Phase 10B.4: per-env routing. When the repo has branch targets, only
    # the push branch's mapped agents are eligible; otherwise fall back to
    # the legacy single tracked branch + all agent paths.
    repo_id = getattr(integration, "repo_id", None)
    branch_targets: list[GithubRepoBranchTarget] = []
    if repo_id:
        branch_targets = session.execute(
            select(GithubRepoBranchTarget).where(
                GithubRepoBranchTarget.repo_id == repo_id
            )
        ).scalars().all()

    eligible_agents: Optional[set[str]] = None  # None = no filter (legacy)
    if branch_targets:
        eligible_agents = {
            bt.agent_name for bt in branch_targets if bt.branch == push_branch
        }
        if not eligible_agents:
            return {
                "status": "ok",
                "event": "push",
                "skipped": "branch_not_tracked",
                "ref": ref,
                "tracked_branches": sorted({bt.branch for bt in branch_targets}),
            }
    elif ref != f"refs/heads/{integration.branch}":
        return {
            "status": "ok",
            "event": "push",
            "skipped": "branch_not_tracked",
            "ref": ref,
            "tracked_branch": integration.branch,
        }

    head_commit = payload.get("head_commit") or {}
    commit_sha = (
        payload.get("after")
        or (head_commit.get("id") if isinstance(head_commit, dict) else None)
        or ""
    )
    if not isinstance(commit_sha, str):
        commit_sha = ""

    commits = payload.get("commits")
    paths = (
        session.execute(
            select(GitHubAgentPath).where(
                GitHubAgentPath.workspace_id == integration.workspace_id
            )
        )
        .scalars()
        .all()
    )

    queued: list[dict[str, Any]] = []
    for ap in paths:
        if eligible_agents is not None and ap.agent_name not in eligible_agents:
            continue  # per-env: this agent isn't mapped to the push branch
        if _push_touched_path(commits or [], ap.path):
            deployment_id = _queue_github_redeploy(
                session,
                integration=integration,
                agent_name=ap.agent_name,
                agent_path=ap.path,
                commit_sha=commit_sha,
            )
            queued.append(
                {
                    "agent_name": ap.agent_name,
                    "commit_sha": commit_sha,
                    "deployment_id": deployment_id,
                }
            )

    # Phase 11.5: enqueue a `polaris.evaluate_push` command so Polaris
    # gets an event-driven dispatch path on every push (in addition to
    # its hourly tick). Polaris's handler reads POLARIS_PUSH_RULES to
    # decide whether to dispatch downstream commands (e.g. atlas.run_tests
    # when backend code changed) without burning a Claude call.
    #
    # Chain id is the GitHub delivery id so every command in the chain
    # — this evaluate_push command, the atlas.run_tests Polaris dispatches,
    # the hermes.post Atlas dispatches — share one chain that the 11.6
    # /dispatch view can render as a single tree.
    delivery_id = (request.headers.get("x-github-delivery") or "").strip()
    chain_id = delivery_id or str(uuid.uuid4())
    touched_paths = _collect_touched_paths(commits or [])
    head_author = (
        head_commit.get("author") if isinstance(head_commit, dict) else None
    )
    push_payload = {
        "commit_sha": commit_sha,
        "branch": push_branch,
        "repo": f"{owner}/{name}",
        "touched_paths": touched_paths,
        "author": head_author if isinstance(head_author, dict) else None,
        "delivery_id": delivery_id or None,
    }
    ensure_agent(session, integration.workspace_id, "polaris", utcnow())
    approval_state = _resolve_auto_approval(
        session,
        workspace_id=integration.workspace_id,
        source_agent=None,
        target_agent="polaris",
        command_kind="polaris.evaluate_push",
    )
    now = utcnow()
    polaris_cmd = Command(
        id=str(uuid.uuid4()),
        workspace_id=integration.workspace_id,
        agent_name="polaris",
        kind="polaris.evaluate_push",
        payload=push_payload,
        status="pending",
        source_agent=None,
        dispatch_chain_id=chain_id,
        dispatch_depth=0,
        approval_state=approval_state,
        approved_at=now if approval_state == "auto_approved" else None,
        created_at=now,
        expires_at=now + COMMAND_TTL,
    )
    session.add(polaris_cmd)
    session.flush()

    return {
        "status": "ok",
        "event": "push",
        "ref": ref,
        "commit_sha": commit_sha,
        "queued_redeploys": queued,
        "polaris_command_id": polaris_cmd.id,
        "dispatch_chain_id": chain_id,
    }


# ---------- /workspaces/me/deployments (Phase 5.1) ---------- #
#
# A "deployment" is a snapshot of bot source code (zipped) plus the lifecycle
# state of one running copy of it. Phase 5.1 only covers the upload + listing
# surface — claim/heartbeat/log endpoints land in 5.2 and the worker process
# in 5.3.


def _serialize_deployment(d: Deployment) -> dict[str, Any]:
    return {
        "id": d.id,
        "agent_name": d.agent_name,
        "status": d.status,
        "desired_state": d.desired_state,
        "source_blob_id": d.source_blob_id,
        "source": d.source,
        "source_commit_sha": d.source_commit_sha,
        "error": d.error,
        "claimed_by": d.claimed_by,
        "claimed_at": d.claimed_at.isoformat() if d.claimed_at else None,
        "heartbeat_at": d.heartbeat_at.isoformat() if d.heartbeat_at else None,
        "started_at": d.started_at.isoformat() if d.started_at else None,
        "stopped_at": d.stopped_at.isoformat() if d.stopped_at else None,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


@app.post("/workspaces/me/deployments")
async def upload_deployment(
    agent_name: str = Form(..., min_length=1),
    bundle: UploadFile = File(...),
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Upload a bundled bot directory as a new deployment.

    The bundle is a gzipped tar (or zip) the SDK CLI produced from a local
    directory. We store the bytes verbatim in deployment_blobs and create a
    `queued` deployment row that the worker (Phase 5.3) will pick up.

    Bundle is read in full so we can compute size + hash up front. The
    multipart cap (10 MB) is enforced by BodySizeLimitMiddleware before
    we get here; this is a defense-in-depth check.
    """
    data = await bundle.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="empty bundle")
    if len(data) > MAX_DEPLOYMENT_BLOB_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"bundle too large; max {MAX_DEPLOYMENT_BLOB_BYTES} bytes",
        )

    now = utcnow()
    sha = hashlib.sha256(data).hexdigest()

    blob = DeploymentBlob(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        size_bytes=len(data),
        sha256=sha,
        data=data,
        created_at=now,
    )
    session.add(blob)
    session.flush()

    ensure_agent(session, workspace_id, agent_name, now)
    _retire_active_deployments_for_agent(
        session, workspace_id=workspace_id, agent_name=agent_name,
    )
    dep = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=blob.id,
        source="cli",
        source_commit_sha=None,
        created_at=now,
        updated_at=now,
    )
    session.add(dep)
    session.flush()
    return _serialize_deployment(dep)


def _has_active_deployment(
    session: Session, workspace_id: str, agent_name: str
) -> bool:
    """True if the agent already has a queued/building/running deployment
    that's meant to stay up (desired_state='running')."""
    row = session.execute(
        select(Deployment.id).where(
            Deployment.workspace_id == workspace_id,
            Deployment.agent_name == agent_name,
            Deployment.desired_state == "running",
            Deployment.status.in_(["queued", "building", "running"]),
        ).limit(1)
    ).first()
    return row is not None


def _deploy_builtin_persona(
    session: Session, workspace_id: str, agent_name: str, now: datetime
) -> "Deployment":
    """Build the vendored persona's bundle, store it as a blob, and queue a
    deployment the worker will pick up. Retires any prior active deployment
    for the agent first (same as the upload path). Does not commit."""
    import builtin_personas

    data = builtin_personas.build_bundle_zip(agent_name)
    blob = DeploymentBlob(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        created_at=now,
    )
    session.add(blob)
    session.flush()

    ensure_agent(session, workspace_id, agent_name, now)
    _retire_active_deployments_for_agent(
        session, workspace_id=workspace_id, agent_name=agent_name,
    )
    dep = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=blob.id,
        source="builtin",
        source_commit_sha=None,
        created_at=now,
        updated_at=now,
    )
    session.add(dep)
    session.flush()
    return dep


@app.post("/workspaces/me/team/deploy")
def deploy_team(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Bring the provisioned built-in personas online.

    The "working team" payoff of onboarding: for each built-in persona that
    has an assistant row in this workspace but no active deployment, build +
    queue its vendored bundle for the worker. Idempotent — personas already
    running are reported, not redeployed — so the welcome page can call it
    safely and a re-run is a no-op.

    Flags `needs_anthropic_key` when an LLM-backed persona was provisioned
    but the workspace has no ANTHROPIC_API_KEY secret, so the dashboard can
    prompt for it (those personas crash gracefully without one).
    """
    import builtin_personas

    now = utcnow()
    agent_names = set(session.execute(
        text("SELECT name FROM agents WHERE workspace_id = :ws"),
        {"ws": workspace_id},
    ).scalars().all())
    provisioned = [p for p in builtin_personas.BUILTIN_PERSONAS
                   if p in agent_names]

    deployed: list[str] = []
    already_running: list[str] = []
    for name in provisioned:
        if _has_active_deployment(session, workspace_id, name):
            already_running.append(name)
            continue
        _deploy_builtin_persona(session, workspace_id, name, now)
        deployed.append(name)
    session.commit()

    has_llm = any(p in builtin_personas.LLM_PERSONAS for p in provisioned)
    has_key = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY")
    ) is not None

    return {
        "deployed": deployed,
        "already_running": already_running,
        "needs_anthropic_key": bool(has_llm and not has_key),
    }


@app.get("/workspaces/me/team/status")
def team_status(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Per-assistant deploy status for the provisioned built-in personas.

    Powers the welcome page's live "your team is coming online" view. For
    each built-in persona with an assistant row, reports the latest
    deployment's status (running / queued / building / stopped) or null if
    it was never deployed. Polled by the dashboard, so it stays a cheap
    single scan.
    """
    import builtin_personas

    agent_names = set(session.execute(
        text("SELECT name FROM agents WHERE workspace_id = :ws"),
        {"ws": workspace_id},
    ).scalars().all())
    provisioned = [p for p in builtin_personas.BUILTIN_PERSONAS
                   if p in agent_names]

    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (agent_name) agent_name, status
              FROM deployments
             WHERE workspace_id = :ws
             ORDER BY agent_name, created_at DESC
            """
        ),
        {"ws": workspace_id},
    ).mappings().all()
    status_by = {r["agent_name"]: r["status"] for r in rows}

    assistants = [
        {
            "name": p,
            "status": status_by.get(p),
            "running": status_by.get(p) == "running",
            "deployed": status_by.get(p) in ("queued", "building", "running"),
            "is_llm": p in builtin_personas.LLM_PERSONAS,
        }
        for p in provisioned
    ]
    has_llm = any(a["is_llm"] for a in assistants)
    has_key = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY")
    ) is not None

    return {
        "assistants": assistants,
        "needs_anthropic_key": bool(has_llm and not has_key),
    }


@app.get("/workspaces/me/deployments")
def list_deployments(
    agent_name: Optional[str] = None,
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List deployments scoped to this workspace, newest first.
    Optionally filter to one agent."""
    limit = max(1, min(limit, 200))
    q = select(Deployment).where(Deployment.workspace_id == workspace_id)
    if agent_name is not None:
        q = q.where(Deployment.agent_name == agent_name)
    rows = session.execute(
        q.order_by(desc(Deployment.created_at)).limit(limit)
    ).scalars().all()
    return {"deployments": [_serialize_deployment(d) for d in rows]}


@app.get("/workspaces/me/deployments/{deployment_id}")
def get_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    d = session.get(Deployment, deployment_id)
    if d is None or d.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    return _serialize_deployment(d)


@app.get("/workspaces/me/deployments/{deployment_id}/logs")
def get_deployment_logs(
    deployment_id: str,
    after_id: int = 0,
    limit: int = 200,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Tail-style log fetch. Pass `after_id` to get only lines newer than the
    last id you saw. `limit` caps the response (1-1000)."""
    limit = max(1, min(limit, 1000))
    dep = session.get(Deployment, deployment_id)
    if dep is None or dep.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    rows = session.execute(
        select(DeploymentLog)
        .where(
            DeploymentLog.deployment_id == deployment_id,
            DeploymentLog.id > after_id,
        )
        .order_by(DeploymentLog.id)
        .limit(limit)
    ).scalars().all()
    return {
        "lines": [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "stream": r.stream,
                "line": r.line,
            }
            for r in rows
        ],
        "max_id": rows[-1].id if rows else after_id,
    }


@app.post("/workspaces/me/deployments/{deployment_id}/stop")
def stop_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Flip desired_state to stopped. The worker's heartbeat picks it up
    within ~30s and terminates the bot."""
    dep = session.get(Deployment, deployment_id)
    if dep is None or dep.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    dep.desired_state = "stopped"
    dep.updated_at = utcnow()
    session.flush()
    return _serialize_deployment(dep)


@app.post("/workspaces/me/deployments/{deployment_id}/redeploy")
def redeploy_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Create a new deployment pointing at the same source blob. The old
    deployment is stopped (desired_state=stopped) so the worker swaps over
    cleanly. Useful for restarting a wedged bot without re-uploading."""
    old = session.get(Deployment, deployment_id)
    if old is None or old.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    if old.source_blob_id is None:
        raise HTTPException(
            status_code=400,
            detail="deployment has no source blob to redeploy from",
        )
    now = utcnow()
    old.desired_state = "stopped"
    old.updated_at = now

    new = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=old.agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=old.source_blob_id,
        # Carry the original provenance forward — a redeploy reuses the
        # same source blob, so the same commit/upload origin still
        # applies. Otherwise a "redeploy" of a github_push deploy would
        # falsely look like a CLI upload in the dashboard.
        source=old.source,
        source_commit_sha=old.source_commit_sha,
        created_at=now,
        updated_at=now,
    )
    session.add(new)
    session.flush()
    return _serialize_deployment(new)


@app.delete("/workspaces/me/deployments/{deployment_id}")
def delete_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    """Delete a deployment row. The associated blob is removed too unless
    other deployments reference it (none should, but the FK uses SET NULL
    so an orphaned blob would just sit until an explicit prune)."""
    d = session.get(Deployment, deployment_id)
    if d is None or d.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    blob_id = d.source_blob_id
    session.delete(d)
    session.flush()  # so the still_used query sees the deletion
    if blob_id:
        # Only purge the blob if no other deployment references it.
        still_used = session.execute(
            select(Deployment.id)
            .where(Deployment.source_blob_id == blob_id)
            .limit(1)
        ).scalar_one_or_none()
        if still_used is None:
            blob = session.get(DeploymentBlob, blob_id)
            if blob is not None:
                session.delete(blob)
    session.flush()
    return {"status": "ok"}


# ---------- /worker/* (Phase 5.2) ---------- #
#
# Worker-facing surface. All endpoints require a bearer matching
# LIGHTSEI_WORKER_TOKEN — see backend/worker_auth.py for the threat model.
# The worker is treated as a trusted system component; a stolen token grants
# cross-tenant access.


class WorkerStatusUpdateIn(BaseModel):
    status: str = Field(min_length=1)
    error: Optional[str] = None


class WorkerLogLineIn(BaseModel):
    ts: Optional[datetime] = None
    stream: str = Field(default="stdout")
    line: str = Field(default="")


class WorkerLogAppendIn(BaseModel):
    lines: list[WorkerLogLineIn] = Field(default_factory=list)


@app.post("/worker/deployments/claim")
def worker_claim_deployment(
    worker_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Atomically grab the oldest queued (or stale-claimed) deployment whose
    user wants it running. SKIP LOCKED matches the pattern used for commands
    and chat turns.

    "Stale-claimed" means another worker took it but hasn't heartbeated within
    WORKER_CLAIM_TTL — almost certainly that worker died. Re-claiming is safe
    because the dead worker can't possibly be running a bot anymore.
    """
    now = utcnow()
    stale_cutoff = now - WORKER_CLAIM_TTL
    row = session.execute(
        text(
            """
            SELECT id FROM deployments
            WHERE desired_state = 'running'
              AND status IN ('queued', 'building', 'running')
              AND (claimed_by IS NULL OR heartbeat_at < :stale_cutoff)
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"stale_cutoff": stale_cutoff},
    ).first()
    if row is None:
        return {"deployment": None}
    dep = session.get(Deployment, row.id)
    if dep is None:
        return {"deployment": None}
    dep.claimed_by = worker_id
    dep.claimed_at = now
    dep.heartbeat_at = now
    if dep.status == "queued":
        dep.status = "building"
    dep.updated_at = now
    session.flush()
    return {
        "deployment": _serialize_deployment(dep),
        "workspace_id": dep.workspace_id,
    }


@app.post("/worker/deployments/{deployment_id}/status")
def worker_update_status(
    deployment_id: str,
    body: WorkerStatusUpdateIn,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Worker reports a status transition. Sets started_at/stopped_at as
    appropriate. Invalid transitions are accepted (server doesn't know what
    the worker is doing) but the status string is checked against the known
    enum to catch typos."""
    valid = {"queued", "building", "running", "stopped", "failed"}
    if body.status not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"unknown status {body.status!r}; must be one of {sorted(valid)}",
        )
    dep = session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    now = utcnow()
    dep.status = body.status
    dep.error = body.error
    dep.heartbeat_at = now
    if body.status == "running" and dep.started_at is None:
        dep.started_at = now
    if body.status in ("stopped", "failed"):
        dep.stopped_at = now
    dep.updated_at = now
    session.flush()
    return _serialize_deployment(dep)


@app.post("/worker/deployments/{deployment_id}/heartbeat")
def worker_heartbeat(
    deployment_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Refresh the worker's claim. Returns the current deployment row so the
    worker can see whether `desired_state` flipped (e.g. user clicked stop)
    without a separate fetch.

    Defense in depth (parking-lot #168): if `dep.status` is terminal
    (`failed` or `stopped`), refuse the update and return 409 instead
    of silently refreshing `heartbeat_at`. Surfaced 2026-05-23 during
    the vela investigation: a worker process had been heartbeating
    a `failed` deployment row for 4 days because nothing in this
    endpoint enforced the terminal-status invariant. The worker's
    heartbeat loop should see the 409 and break itself; if not, at
    least the row stops drifting + the audit query in #64 won't be
    misled again."""
    dep = session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if dep.status in ("failed", "stopped"):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "deployment_terminal",
                "deployment_id": deployment_id,
                "status": dep.status,
                "desired_state": dep.desired_state,
                "message": (
                    f"deployment is in terminal status "
                    f"{dep.status!r}; the worker should stop "
                    "heartbeating this deployment."
                ),
            },
        )
    dep.heartbeat_at = utcnow()
    session.flush()
    return _serialize_deployment(dep)


@app.post("/worker/deployments/{deployment_id}/logs")
def worker_append_logs(
    deployment_id: str,
    body: WorkerLogAppendIn,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, int]:
    """Append log lines. Bounded to MAX_DEPLOYMENT_LOG_LINES per deployment
    by deleting the oldest rows after each insert."""
    dep = session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if not body.lines:
        return {"appended": 0}
    now = utcnow()
    rows = [
        DeploymentLog(
            deployment_id=deployment_id,
            ts=line.ts or now,
            stream=line.stream,
            line=line.line,
        )
        for line in body.lines
    ]
    session.add_all(rows)
    session.flush()
    # Prune older rows beyond the cap. Keep this cheap: count + DELETE.
    count = session.execute(
        select(text("COUNT(*)")).select_from(DeploymentLog)
        .where(DeploymentLog.deployment_id == deployment_id)
    ).scalar_one()
    overflow = count - MAX_DEPLOYMENT_LOG_LINES
    if overflow > 0:
        session.execute(
            text(
                """
                DELETE FROM deployment_logs
                WHERE id IN (
                  SELECT id FROM deployment_logs
                  WHERE deployment_id = :dep
                  ORDER BY id ASC LIMIT :n
                )
                """
            ),
            {"dep": deployment_id, "n": overflow},
        )
    return {"appended": len(rows)}


@app.get("/worker/blobs/{blob_id}")
def worker_get_blob(
    blob_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
):
    """Return the raw bytes of a deployment blob (the zipped bot directory)."""
    blob = session.get(DeploymentBlob, blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="blob not found")
    from fastapi.responses import Response as RawResponse
    return RawResponse(
        content=blob.data,
        media_type="application/octet-stream",
        headers={
            "x-lightsei-blob-sha256": blob.sha256,
            "x-lightsei-blob-size": str(blob.size_bytes),
        },
    )


@app.get("/worker/workspaces/{workspace_id}/secrets")
def worker_list_workspace_secrets(
    workspace_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Decrypted secrets for a workspace, indexed by name. The worker
    injects these as env vars when spawning a bot."""
    if not secrets_crypto.is_available():
        raise HTTPException(
            status_code=503,
            detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured",
        )
    rows = session.execute(
        select(WorkspaceSecret).where(WorkspaceSecret.workspace_id == workspace_id)
    ).scalars().all()
    out: dict[str, str] = {}
    for s in rows:
        try:
            out[s.name] = secrets_crypto.decrypt(s.encrypted_value)
        except Exception:
            # A single corrupt row should not poison the rest of the dict.
            # The user will see the bot fail to read this secret and can
            # re-set it from the dashboard.
            continue

    # Phase 33.3: inject the workspace's business context as an env var
    # (NOT a stored secret) so deployed personas can tailor their voice to
    # the owner's industry. Piggy-backs on this dict because the worker
    # injects the whole thing as the bot's env, avoiding a second
    # round-trip. Never overrides a real secret of the same name.
    prof_row = session.execute(
        text("SELECT onboarding_profile FROM workspaces WHERE id = :ws"),
        {"ws": workspace_id},
    ).first()
    if prof_row and prof_row[0]:
        industry = (prof_row[0] or {}).get("industry")
        if industry and "LIGHTSEI_BUSINESS_INDUSTRY" not in out:
            out["LIGHTSEI_BUSINESS_INDUSTRY"] = str(industry)

    return {"secrets": out}


# ---------- /auth ---------- #

def _create_session(session: Session, user: User) -> tuple[SessionRow, str]:
    plaintext = generate_session_token()
    now = utcnow()
    row = SessionRow(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token_hash=hash_token(plaintext),
        created_at=now,
        expires_at=now + SESSION_TTL,
        # Phase 23.2: new sessions land in the user's legacy/primary
        # workspace. Phase 23.3+ lets the operator switch via the
        # header dropdown; until then this is the only workspace they
        # have anyway.
        active_workspace_id=user.workspace_id,
    )
    session.add(row)
    session.flush()
    return row, plaintext


def _serialize_user(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "email": u.email,
        "workspace_id": u.workspace_id,
        "created_at": u.created_at.isoformat(),
    }


@app.post("/auth/signup")
def signup(
    body: SignupIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    limit_signup_attempt(request)
    existing = session.execute(
        select(User).where(User.email == body.email.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    now = utcnow()
    ws = Workspace(id=str(uuid.uuid4()), name=body.workspace_name, created_at=now)
    session.add(ws)
    session.flush()
    seed_default_validators(session, ws.id, now)

    user = User(
        id=str(uuid.uuid4()),
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        workspace_id=ws.id,
        created_at=now,
    )
    session.add(user)
    session.flush()  # need user.id present before sessions row FKs to it

    # Phase 23.2: workspace_members is the new source of truth for
    # "this user belongs to this workspace." Insert here so the new
    # session-auth resolver (auth.py) accepts the freshly-minted
    # session on the very next request.
    session.add(WorkspaceMember(user_id=user.id, workspace_id=ws.id))

    plaintext_key = generate_key()
    api_key_row = ApiKey(
        id=str(uuid.uuid4()),
        workspace_id=ws.id,
        name="default",
        prefix=prefix_for_display(plaintext_key),
        hash=hash_token(plaintext_key),
        created_at=now,
    )
    session.add(api_key_row)

    sess_row, sess_plain = _create_session(session, user)

    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws),
        "api_key": _serialize_api_key(api_key_row) | {"plaintext": plaintext_key},
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
    }


@app.post("/auth/login")
def login(
    body: LoginIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    limit_login_attempt(request)
    user = session.execute(
        select(User).where(User.email == body.email.lower())
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    sess_row, sess_plain = _create_session(session, user)
    ws = session.get(Workspace, user.workspace_id)
    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws) if ws else None,
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
    }


@app.post("/auth/logout")
def logout(
    auth: AuthResult = Depends(get_authenticated),
) -> dict[str, Any]:
    if auth.session is None:
        raise HTTPException(status_code=400, detail="logout requires a session token")
    if auth.session.revoked_at is None:
        auth.session.revoked_at = utcnow()
    return {"status": "ok"}


@app.delete("/auth/account")
def delete_my_account(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 31.5.g: in-app account deletion for operators.

    Apple guideline 5.1.1(v) requires any app that lets users create an
    account to let them delete it from inside the app. Session-only: an
    api key has no user identity to delete.

    Capture the workspaces this user owns first (deleting the user
    cascades away the workspace_members rows that record ownership),
    delete the user (cascade removes their sessions + memberships and
    nulls the audit pointers that reference them), then delete each
    owned workspace (cascade removes its agents / runs / events /
    deployments / remaining members). Hard delete, no grace period in
    v1; the user re-signs-up to start over.
    """
    _require_session_user(auth)
    user_id = auth.user.id

    owned_ids = session.execute(
        select(WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id)
        .where(WorkspaceMember.role == "owner")
    ).scalars().all()

    user = session.get(User, user_id)
    if user is not None:
        session.delete(user)
    session.flush()

    for ws_id in owned_ids:
        ws = session.get(Workspace, ws_id)
        if ws is not None:
            session.delete(ws)
    session.flush()

    return {"deleted": True}


# ---------- Phase 17.2: magic-link auth ---------- #


# How many magic-link requests we accept for a single email per hour.
# Tight enough that a malicious sender can't spam someone's inbox,
# loose enough that a confused user clicking "send again" 2-3 times
# in a row still works.
MAGIC_LINK_MAX_PER_HOUR = 5

# How long a fresh token is valid. 15 minutes is plenty for the user
# to switch to their email and click; short enough that a leaked
# email backup doesn't hand attackers active sign-in.
MAGIC_LINK_TTL = timedelta(minutes=15)


def _hash_magic_token(token: str) -> str:
    """Same sha256-hex pattern keys.hash_token uses. Defined here to
    avoid coupling the magic-link flow to the API-key key derivation —
    if either changes the other shouldn't have to follow."""
    import hashlib as _hashlib
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()


def _dashboard_base_url() -> str:
    """Where the magic-link URL points. Env-overridable so local dev
    can use localhost; defaults to the prod dashboard."""
    return os.environ.get(
        "LIGHTSEI_DASHBOARD_URL", "https://app.lightsei.com",
    )


def _workspace_name_for_signup_email(email: str) -> str:
    """A friendly default workspace name from the user's email local
    part — `alice@example.com` → "alice's workspace". The user can
    rename via /account afterward."""
    local = email.split("@", 1)[0]
    return f"{local}'s workspace"


@app.post("/auth/magic-link/request")
def request_magic_link(
    body: MagicLinkRequestIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.2: kick off a magic-link sign-in.

    Always returns 200 — don't leak whether the email is registered.
    Rate-limited per-IP (existing limit_signup_attempt machinery)
    AND per-email (count in the last hour vs MAGIC_LINK_MAX_PER_HOUR)
    so a malicious sender can't spam a single inbox via rotating IPs.

    On success, inserts an email_signin_tokens row with a 15-minute
    TTL and sends a Resend email containing the unhashed token in a
    magic URL. The dashboard's /auth/magic-link page POSTs the
    token back to /auth/magic-link/consume.
    """
    import email_provider as _email_mod

    limit_signup_attempt(request)
    email_addr = body.email.lower()
    now = utcnow()

    # Per-email rate-limit: count tokens we've issued in the last hour.
    # An hour is enough that 5 attempts feels generous; a malicious
    # sender hitting the limit means the user already has 5 fresh
    # magic links in their inbox.
    cutoff = now - timedelta(hours=1)
    recent_count = session.execute(
        select(func.count(EmailSigninToken.token_hash)).where(
            EmailSigninToken.email == email_addr,
            EmailSigninToken.created_at >= cutoff,
        )
    ).scalar_one()
    if recent_count >= MAGIC_LINK_MAX_PER_HOUR:
        # Same always-200 contract so we don't leak that this email
        # is being targeted.
        return {"status": "ok"}

    # Generate token + insert hashed row. Same plain-token-in-URL +
    # hashed-row-in-DB pattern the existing API keys use.
    plaintext = _stdlib_secrets.token_urlsafe(32)
    token_hash = _hash_magic_token(plaintext)
    session.add(EmailSigninToken(
        token_hash=token_hash,
        email=email_addr,
        created_at=now,
        expires_at=now + MAGIC_LINK_TTL,
    ))
    session.flush()

    try:
        _email_mod.send_magic_link(
            email=email_addr,
            token=plaintext,
            dashboard_url=_dashboard_base_url(),
        )
    except Exception:
        # Best-effort email send. Logged inside the module. We still
        # return 200 to preserve the no-leak contract; user can
        # retry from the dashboard if the email never arrives.
        import logging as _logging
        _logging.getLogger("lightsei.auth").warning(
            "magic-link request: send failed for %s — token still "
            "valid via direct backend POST",
            email_addr,
        )
    return {"status": "ok"}


@app.post("/auth/magic-link/consume")
def consume_magic_link(
    body: MagicLinkConsumeIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.2: consume a magic-link token, sign in (or create) the
    user, return a session.

    422 on unknown / expired / consumed token (don't distinguish — the
    user can't action the difference and we don't want token-validity
    probes to leak existence). 200 with a session token on success.

    New-user path creates a User + Workspace pair the same way the
    existing /auth/signup does, minus the password (auth_provider =
    'magic_link', email_verified = True since they proved control of
    the inbox). No API key is auto-created — non-technical users don't
    need one; developers can generate one from /account.
    """
    limit_login_attempt(request)
    token_hash = _hash_magic_token(body.token)
    now = utcnow()

    row = session.get(EmailSigninToken, token_hash)
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        # Single 422 covers all the rejection paths so the client
        # can't probe for "exists but expired" vs "never existed".
        raise HTTPException(
            status_code=422,
            detail="magic-link token is invalid or expired — request a new one",
        )

    # Mark consumed before doing anything else so a parallel POST with
    # the same token gets rejected by the next check. Single-use.
    row.consumed_at = now
    session.flush()

    email_addr = row.email
    user = session.execute(
        select(User).where(User.email == email_addr)
    ).scalar_one_or_none()
    is_new_user = user is None

    if user is None:
        # Fresh signup via magic link. Workspace gets the same starting
        # state Phase 17.1's backfill applied to existing rows: free
        # tier + $5 in credits. No API key auto-generated — magic-link
        # users land on the dashboard, not the SDK.
        ws = Workspace(
            id=str(uuid.uuid4()),
            name=_workspace_name_for_signup_email(email_addr),
            created_at=now,
            plan_tier="free",
            free_credits_remaining_usd=Decimal("5.00"),
        )
        session.add(ws)
        session.flush()
        seed_default_validators(session, ws.id, now)

        user = User(
            id=str(uuid.uuid4()),
            email=email_addr,
            # No password — magic-link users can't fall back to the
            # password login. A non-empty placeholder so the NOT NULL
            # constraint passes; it's not a real bcrypt hash so the
            # /auth/login verify_password will fail-closed for them.
            password_hash="magic-link-only:no-password",
            workspace_id=ws.id,
            created_at=now,
            email_verified=True,
            auth_provider="magic_link",
        )
        session.add(user)
        session.flush()
        # Phase 23.2: see the apikey signup site for the same insert.
        session.add(WorkspaceMember(user_id=user.id, workspace_id=ws.id))
    else:
        # Existing user signing in via magic link — promote them to
        # verified if they weren't already. Doesn't change the original
        # auth_provider; the user keeps whatever path created them.
        if not user.email_verified:
            user.email_verified = True

    sess_row, sess_plain = _create_session(session, user)
    ws = session.get(Workspace, user.workspace_id)

    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws) if ws else None,
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
        "is_new_user": is_new_user,
    }


# ---------- Phase 25.2: end-user magic-link auth ---------- #


# Same caps + TTL as the operator flow. Reusing the constants would
# couple the two flows in a way that makes future tuning awkward
# (consumer signup might want a different rate ceiling than operator
# signin), so they're spelled separately on purpose.
END_USER_MAGIC_LINK_MAX_PER_HOUR = 5
END_USER_MAGIC_LINK_TTL = timedelta(minutes=15)
# End-user sessions live the same 30 days as operator sessions. Long
# enough that a returning visitor isn't constantly re-authing; short
# enough that a stale device gets cycled out within a reasonable
# window.
END_USER_SESSION_TTL = timedelta(days=30)


def _serialize_end_user(eu: EndUser) -> dict[str, Any]:
    return {
        "id": eu.id,
        "email": eu.email,
        "display_name": eu.display_name,
        "email_verified": eu.email_verified,
        "auth_provider": eu.auth_provider,
        "created_at": eu.created_at.isoformat(),
    }


def _create_end_user_session(
    session: Session, end_user: EndUser,
) -> tuple[EndUserSession, str]:
    """Parallel to `_create_session` for operators but keyed off
    EndUser. Returns (row, plaintext) — caller serializes plaintext
    into the response; the DB only ever sees the hash."""
    plaintext = generate_session_token()
    now = utcnow()
    row = EndUserSession(
        id=str(uuid.uuid4()),
        end_user_id=end_user.id,
        token_hash=hash_token(plaintext),
        created_at=now,
        expires_at=now + END_USER_SESSION_TTL,
    )
    session.add(row)
    session.flush()
    return row, plaintext


@app.post("/auth/end-user/magic-link/request")
def request_end_user_magic_link(
    body: EndUserMagicLinkRequestIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 25.2: kick off an end-user magic-link sign-in / signup.

    Mirrors the operator `/auth/magic-link/request` no-leak contract:
    always 200, per-email rate-limited (5/hour) so a malicious sender
    can't spam a single inbox via rotating IPs, per-IP limited via
    `limit_signup_attempt`. Stores the token hash on
    `end_user_signin_tokens` with a 15-minute TTL; sends the unhashed
    token in a Resend magic URL pointing at the end-user landing page.

    `vendor_invite_code` carries through to consume side (Phase 27.2
    is where the link actually gets created); today we just persist
    it on the token row so the round-trip preserves it.
    """
    import email_provider as _email_mod

    limit_signup_attempt(request)
    email_addr = body.email.lower()
    now = utcnow()

    # Per-email rate-limit, same shape as operator flow.
    cutoff = now - timedelta(hours=1)
    recent_count = session.execute(
        select(func.count(EndUserSigninToken.token_hash)).where(
            EndUserSigninToken.email == email_addr,
            EndUserSigninToken.created_at >= cutoff,
        )
    ).scalar_one()
    if recent_count >= END_USER_MAGIC_LINK_MAX_PER_HOUR:
        return {"status": "ok"}

    plaintext = _stdlib_secrets.token_urlsafe(32)
    token_hash = _hash_magic_token(plaintext)
    session.add(EndUserSigninToken(
        token_hash=token_hash,
        email=email_addr,
        created_at=now,
        expires_at=now + END_USER_MAGIC_LINK_TTL,
        vendor_invite_code=body.vendor_invite_code,
    ))
    session.flush()

    try:
        _email_mod.send_end_user_magic_link(
            email=email_addr,
            token=plaintext,
            dashboard_url=_dashboard_base_url(),
        )
    except Exception:
        import logging as _logging
        _logging.getLogger("lightsei.auth").warning(
            "end-user magic-link request: send failed for %s, "
            "token still valid via direct backend POST",
            email_addr,
        )
    return {"status": "ok"}


@app.post("/auth/end-user/magic-link/consume")
def consume_end_user_magic_link(
    body: EndUserMagicLinkConsumeIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 25.2: consume an end-user magic-link token. Sign in
    the matching end_user, or create a fresh end_user row on first
    use (signup-via-magic-link).

    422 covers invalid / expired / consumed, single-line message so
    the client can't probe token validity by reading detail strings.
    On success, mints an `end_user_sessions` row + returns the
    plaintext bearer token.

    `vendor_invite_code` on the response carries whatever code was
    attached to the original request OR the override sent on this
    consume call (the consume body wins if both are set). Today the
    code is just echoed back, not acted on; Phase 27.2 reads it and
    inserts an `end_user_vendor_links` row in the same transaction.
    """
    limit_login_attempt(request)
    token_hash = _hash_magic_token(body.token)
    now = utcnow()

    row = session.get(EndUserSigninToken, token_hash)
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        raise HTTPException(
            status_code=422,
            detail="magic-link token is invalid or expired, request a new one",
        )

    # Mark consumed before doing the find-or-create so a parallel
    # POST with the same token loses the race + 422s.
    row.consumed_at = now
    session.flush()

    email_addr = row.email
    end_user = session.execute(
        select(EndUser).where(EndUser.email == email_addr)
    ).scalar_one_or_none()
    is_new_end_user = end_user is None

    if end_user is None:
        end_user = EndUser(
            id=str(uuid.uuid4()),
            email=email_addr,
            email_verified=True,
            auth_provider="magic_link",
            created_at=now,
            updated_at=now,
        )
        session.add(end_user)
        session.flush()
    else:
        if not end_user.email_verified:
            end_user.email_verified = True
            end_user.updated_at = now

    sess_row, sess_plain = _create_end_user_session(session, end_user)

    # The body's vendor_invite_code wins over the request-side one
    # so a user who skipped the field at request time can supply it
    # at consume time without re-issuing the magic link.
    invite_code = body.vendor_invite_code or row.vendor_invite_code

    return {
        "end_user": _serialize_end_user(end_user),
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
        "is_new_end_user": is_new_end_user,
        "vendor_invite_code": invite_code,
        # Phase 27.2 will replace this stub with the real linked
        # workspaces list once the invite-code table lands. Empty
        # list keeps the response shape stable for the dashboard
        # to consume now.
        "linked_vendors": [],
    }


# ---------- Phase 26.2: end-user vendor + conversation endpoints ---------- #


# Conversation-list cap. Larger than Phase 21.8's operator-side cap
# because end users typically have FEWER active threads with a vendor
# (one consumer-side conversation tends to span weeks/months) so even
# the heaviest user is unlikely to exceed 50. Pagination parks to
# Phase 26B if usage proves otherwise.
END_USER_CONVERSATION_LIST_LIMIT = 50


def _serialize_vendor_for_end_user(w: Workspace) -> dict[str, Any]:
    """Trimmed vendor projection for the consumer surface. Returns
    only fields the end user has any business seeing — no internal
    workspace settings, no billing surface, no plan tier."""
    return {
        "id": w.id,
        "name": w.name,
        "vendor_slug": w.vendor_slug,
        # widget_public_id is the handle the existing POST /widget/...
        # endpoints already accept, so the dashboard can call those
        # directly without an extra slug-to-public-id lookup hop on
        # every send/poll.
        "widget_public_id": w.widget_public_id,
        # customer_facing_agent_name tells the dashboard which bot
        # the conversations are with (rendered as "Chat with vega"
        # in the per-vendor header).
        "customer_facing_agent_name": w.customer_facing_agent_name,
    }


@app.get("/me/end-user")
def me_end_user(
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 26.2: who-am-I for the consumer surface.

    Returns the signed-in EndUser + the vendors they're actively
    subscribed to (`end_user_vendor_links.removed_at IS NULL`).
    `/c` calls this on load to render the vendor cards; `/c/{slug}`
    calls it on load to identify the user + verify they're linked
    to the slug they're trying to chat with.

    Phase 28.5 extension: surfaces the VAPID public key + whether
    the end user already has an active push subscription, so the
    /c EnablePushPrompt component can render the right state in
    one fetch.
    """
    import push as _push

    has_active_push = session.scalar(
        select(EndUserPushSubscription.id)
        .where(
            EndUserPushSubscription.end_user_id == auth.end_user.id,
            EndUserPushSubscription.revoked_at.is_(None),
        )
        .limit(1)
    ) is not None

    return {
        "end_user": _serialize_end_user(auth.end_user),
        "linked_vendors": [
            _serialize_vendor_for_end_user(w)
            for w in auth.linked_workspaces
        ],
        "push_vapid_public_key": _push.get_vapid_public_key(),
        "has_active_push_subscription": has_active_push,
    }


@app.delete("/me/end-user")
def delete_my_end_user_account(
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 31.5.g: in-app account deletion for end users.

    Apple guideline 5.1.1(v), same requirement as the operator path.
    Deleting the EndUser row cascades their sessions, vendor links,
    push subscriptions, and APNS tokens via the existing FKs. Widget
    conversations keep their transcript with `end_user_id` set NULL
    (the column is ON DELETE SET NULL) so the vendor's history /
    analytics aren't punched full of holes; the row no longer points
    back at a person. Hard delete, no grace period in v1.
    """
    end_user = session.get(EndUser, auth.end_user.id)
    if end_user is not None:
        session.delete(end_user)
    session.flush()
    return {"deleted": True}


@app.get("/me/end-user/vendors/{slug}")
def me_end_user_vendor(
    slug: str,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 26.2 + Phase 27.5: resolve a vendor by its `vendor_slug`
    for the consumer chat surface.

    404 if no vendor has this slug OR the end user isn't linked to
    it. Same 404 shape for both so the response doesn't leak which
    slugs exist on Lightsei to a curious authenticated user.

    Phase 27.5 extension: also returns the end user's per-vendor
    link settings (`notification_pref`, `display_name_override`) so
    the /c/{slug}/settings page can render the form pre-populated
    in one fetch.
    """
    vendor = next(
        (w for w in auth.linked_workspaces if w.vendor_slug == slug),
        None,
    )
    if vendor is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "vendor_not_found",
                "message": (
                    f"no vendor with slug {slug!r} is linked to your account"
                ),
            },
        )
    link = session.get(
        EndUserVendorLink, (auth.end_user.id, vendor.id),
    )
    out = _serialize_vendor_for_end_user(vendor)
    out["notification_pref"] = (
        link.notification_pref if link else "all"
    )
    out["display_name_override"] = (
        link.display_name_override if link else None
    )
    return out


@app.get("/me/end-user/vendors/{slug}/conversations")
def me_end_user_vendor_conversations(
    slug: str,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 26.2: list the end user's widget conversations with one
    vendor.

    Same 404 contract as the vendor-resolve endpoint: not linked OR
    no such slug → 404 (no probing). Conversations are filtered by
    `widget_conversations.end_user_id == auth.end_user.id` AND
    `workspace_id == vendor.id` so a leaked conversation id from
    another vendor can't be polled here.

    Sorted by `last_message_at desc`, capped at
    `END_USER_CONVERSATION_LIST_LIMIT`. Pagination + per-conversation
    last-message preview park to Phase 26B if needed.
    """
    vendor: Optional[Workspace] = None
    for w in auth.linked_workspaces:
        if w.vendor_slug == slug:
            vendor = w
            break
    if vendor is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "vendor_not_found",
                "message": (
                    f"no vendor with slug {slug!r} is linked to your account"
                ),
            },
        )

    rows = session.execute(
        select(WidgetConversation)
        .where(
            WidgetConversation.workspace_id == vendor.id,
            WidgetConversation.end_user_id == auth.end_user.id,
        )
        .order_by(WidgetConversation.last_message_at.desc())
        .limit(END_USER_CONVERSATION_LIST_LIMIT)
    ).scalars().all()

    return {
        "vendor": _serialize_vendor_for_end_user(vendor),
        "conversations": [
            {
                "id": c.id,
                "status": c.status,
                "customer_facing_agent_name": c.customer_facing_agent_name,
                "started_at": c.started_at.isoformat(),
                "last_message_at": c.last_message_at.isoformat(),
                "resolved_at": (
                    c.resolved_at.isoformat() if c.resolved_at else None
                ),
            }
            for c in rows
        ],
    }


# ---------- Phase 27.2: vendor invite codes + per-vendor end-user settings ---------- #


# Cap on how many codes a single mint request can issue. Higher than
# the practical "one customer at a time" use case (operators rarely
# need >10), but low enough that a misclick can't dump a million
# rows into vendor_invite_codes.
VENDOR_INVITE_MINT_CAP = 100


def _serialize_invite_code(c: VendorInviteCode) -> dict[str, Any]:
    return {
        "code": c.code,
        "workspace_id": c.workspace_id,
        "created_at": c.created_at.isoformat(),
        "expires_at": c.expires_at.isoformat(),
        "consumed_at": (
            c.consumed_at.isoformat() if c.consumed_at else None
        ),
        "consumed_by_end_user_id": c.consumed_by_end_user_id,
    }


@app.post("/workspaces/me/end-user-invites")
def mint_vendor_invite_codes(
    body: VendorInviteMintIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 27.2: operator mints N single-use invite codes.

    Codes are UUID-shaped + 30-day TTL by default. The response is
    the only place the plaintext is shown — Phase 27.3's UI tells
    the operator to copy them now. (We could also re-fetch but the
    "shown once" pattern matches API-key minting from Phase 17.)
    """
    now = utcnow()
    expires_at = now + timedelta(days=body.ttl_days)
    minted: list[VendorInviteCode] = []
    for _ in range(body.count):
        code = f"inv-{uuid.uuid4()}"
        row = VendorInviteCode(
            code=code,
            workspace_id=workspace_id,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        minted.append(row)
    session.flush()
    return {
        "codes": [_serialize_invite_code(c) for c in minted],
    }


@app.get("/workspaces/me/end-user-invites")
def list_vendor_invite_codes(
    include_consumed: bool = False,
    include_expired: bool = False,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 27.2: list invite codes for the operator's workspace.

    Default = only outstanding codes (not consumed, not expired).
    Query params `?include_consumed=true` / `?include_expired=true`
    widen the result; the Phase 27.3 UI uses the default for the
    "still redeemable" surface and includes both for the full audit
    log.
    """
    now = utcnow()
    q = select(VendorInviteCode).where(
        VendorInviteCode.workspace_id == workspace_id
    )
    if not include_consumed:
        q = q.where(VendorInviteCode.consumed_at.is_(None))
    if not include_expired:
        q = q.where(VendorInviteCode.expires_at > now)
    q = q.order_by(VendorInviteCode.created_at.desc())

    rows = session.execute(q).scalars().all()
    return {
        "codes": [_serialize_invite_code(c) for c in rows],
    }


@app.delete("/workspaces/me/end-user-invites/{code}")
def revoke_vendor_invite_code(
    code: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 27.2: revoke an unconsumed invite code.

    Hard-delete. A consumed code returns 404 (since revoking a
    consumed code is a no-op anyway — the end-user link already
    exists). A code from a different workspace also returns 404
    (no-leak; operator A shouldn't be able to probe whether op B
    has a specific code).
    """
    row = session.get(VendorInviteCode, code)
    if (
        row is None
        or row.workspace_id != workspace_id
        or row.consumed_at is not None
    ):
        raise HTTPException(
            status_code=404,
            detail={"error": "invite_code_not_found"},
        )
    session.delete(row)
    return {"revoked": True, "code": code}


@app.post("/me/end-user/redeem-invite")
def redeem_vendor_invite_code(
    body: VendorInviteRedeemIn,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 27.2: end user redeems an invite code, getting linked
    to the vendor that issued it.

    422 on invalid / expired / already-consumed code (single error
    shape so a probe can't distinguish). On success the code's
    `consumed_at` + `consumed_by_end_user_id` get set + an
    `end_user_vendor_links` row is created (or re-activated if a
    soft-removed row exists). Idempotent re-redeem within the same
    workspace + end-user pair returns 200 with the existing link;
    re-consuming the same code path is blocked by the consumed_at
    check above.
    """
    now = utcnow()
    code_row = session.get(VendorInviteCode, body.code)
    if (
        code_row is None
        or code_row.consumed_at is not None
        or code_row.expires_at <= now
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invite_code_invalid",
                "message": (
                    "invite code is invalid or expired, ask the vendor "
                    "to send a fresh one"
                ),
            },
        )

    # Mark consumed before doing the link insert so a parallel
    # redeem of the same code loses the race + 422s.
    code_row.consumed_at = now
    code_row.consumed_by_end_user_id = auth.end_user.id
    session.flush()

    existing = session.get(
        EndUserVendorLink, (auth.end_user.id, code_row.workspace_id),
    )
    if existing is not None:
        # Already linked. If soft-revoked, re-activate by clearing
        # removed_at. Otherwise no-op.
        if existing.removed_at is not None:
            existing.removed_at = None
        link = existing
    else:
        link = EndUserVendorLink(
            end_user_id=auth.end_user.id,
            workspace_id=code_row.workspace_id,
            linked_via="invite_code",
            linked_at=now,
        )
        session.add(link)
        session.flush()

    ws = session.get(Workspace, code_row.workspace_id)
    return {
        "linked": True,
        "vendor": (
            _serialize_vendor_for_end_user(ws) if ws else None
        ),
        "link": {
            "linked_at": link.linked_at.isoformat(),
            "linked_via": link.linked_via,
            "notification_pref": link.notification_pref,
            "display_name_override": link.display_name_override,
        },
    }


@app.get("/me/end-user/vendors")
def list_end_user_vendors(
    auth: EndUserAuthResult = Depends(get_end_user),
) -> dict[str, Any]:
    """Phase 27.2: list the end user's actively-linked vendors with
    placeholder unread counts.

    Note: `unread_count` is hard-coded to 0 in v1. Proper per-vendor
    last-seen tracking + unread counting parks as a Phase 27B
    follow-up (needs a `last_seen_at` column on
    end_user_vendor_links). The field is on the response shape now
    so the Phase 27.4 my-bots dashboard can render the badge slot
    without a future schema-renumber.
    """
    return {
        "vendors": [
            {
                **_serialize_vendor_for_end_user(w),
                "unread_count": 0,
            }
            for w in auth.linked_workspaces
        ],
    }


@app.patch("/me/end-user/vendors/{workspace_id}")
def patch_end_user_vendor(
    workspace_id: str,
    body: EndUserVendorPatchIn,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 27.2: end user updates their per-vendor settings
    (notification_pref + display_name_override).

    404 if the end user isn't linked to this workspace. 422 on
    invalid notification_pref (server-side re-validated via
    is_valid_notification_pref). Empty-string display_name_override
    clears the override (falls back to end_users.display_name on
    read paths).
    """
    link = session.get(EndUserVendorLink, (auth.end_user.id, workspace_id))
    if link is None or link.removed_at is not None:
        raise HTTPException(
            status_code=404,
            detail={"error": "vendor_link_not_found"},
        )

    fields = body.model_fields_set
    if "notification_pref" in fields:
        if body.notification_pref is None:
            # Reset to default rather than NULL (column is NOT NULL).
            link.notification_pref = "all"
        else:
            if not is_valid_notification_pref(body.notification_pref):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "invalid_notification_pref",
                        "valid": sorted(["all", "mentions", "off"]),
                    },
                )
            link.notification_pref = body.notification_pref
    if "display_name_override" in fields:
        if not body.display_name_override:
            # Empty string OR explicit null → clear the override.
            link.display_name_override = None
        else:
            link.display_name_override = body.display_name_override.strip()

    session.flush()
    return {
        "workspace_id": workspace_id,
        "notification_pref": link.notification_pref,
        "display_name_override": link.display_name_override,
    }


@app.delete("/me/end-user/vendors/{workspace_id}")
def soft_revoke_end_user_vendor(
    workspace_id: str,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 27.2: end user unsubscribes from a vendor. Soft-revoke
    (sets `removed_at`), not hard-delete.

    Per Phase 27 spec: past conversations stay readable to the end
    user (read-only); no new messages can be sent. Today (pre-27.2)
    the read-only nuance is parked to a follow-up; for now,
    unlinking removes the vendor from the active-list endpoints
    (already filtered on `removed_at IS NULL`).

    404 if not currently linked (or already-removed) so the
    revoke is idempotent: a second DELETE returns 404, not a
    silent OK.
    """
    link = session.get(EndUserVendorLink, (auth.end_user.id, workspace_id))
    if link is None or link.removed_at is not None:
        raise HTTPException(
            status_code=404,
            detail={"error": "vendor_link_not_found"},
        )
    link.removed_at = utcnow()
    session.flush()
    return {"unlinked": True, "workspace_id": workspace_id}


# ---------- Phase 28.5: end-user push subscriptions ---------- #


@app.post("/me/end-user/push-subscriptions")
def create_end_user_push_subscription(
    body: EndUserPushSubscriptionIn,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 28.5: end user enables push notifications on a device.

    The browser calls `PushManager.subscribe({applicationServerKey})`
    and hands the resulting endpoint + p256dh + auth back here. The
    composite unique constraint on (end_user_id, endpoint) means a
    re-subscribe from the same device upserts the row: any existing
    row is updated in-place (new keys, revoked_at cleared) so the
    Phase 28.2 send fan-out picks it up again.

    No 410-cleanup race here: 410 cleanup runs on send-failure and
    sets revoked_at, but a fresh subscribe always clears it.
    """
    existing = session.scalar(
        select(EndUserPushSubscription).where(
            EndUserPushSubscription.end_user_id == auth.end_user.id,
            EndUserPushSubscription.endpoint == body.endpoint,
        )
    )
    if existing is not None:
        existing.p256dh = body.p256dh
        existing.auth = body.auth
        existing.revoked_at = None
        row = existing
    else:
        row = EndUserPushSubscription(
            id=str(uuid.uuid4()),
            end_user_id=auth.end_user.id,
            endpoint=body.endpoint,
            p256dh=body.p256dh,
            auth=body.auth,
        )
        session.add(row)
    session.flush()
    return {
        "id": row.id,
        "endpoint": row.endpoint,
        "active": row.revoked_at is None,
    }


@app.delete("/me/end-user/push-subscriptions")
def revoke_end_user_push_subscription(
    body: EndUserPushUnsubscribeIn,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 28.5: end user turns off push on a device.

    Browser calls `PushManager.unsubscribe()` first, then POSTs the
    endpoint back here so we set `revoked_at` on the matching row.
    Soft-revoke (not hard-delete) so 410-cleanup + audit history
    stays coherent; the partial active index excludes revoked rows
    from the send fan-out.

    404 if the endpoint doesn't match a row owned by this end user
    (cross-end-user isolation: the WHERE clause filters on
    end_user_id, so another user's endpoint string is invisible).
    """
    row = session.scalar(
        select(EndUserPushSubscription).where(
            EndUserPushSubscription.end_user_id == auth.end_user.id,
            EndUserPushSubscription.endpoint == body.endpoint,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "push_subscription_not_found"},
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.flush()
    return {"revoked": True, "endpoint": row.endpoint}


# ---------- Phase 29.4 stub: end-user APNS device tokens ---------- #


@app.post("/me/end-user/apns-tokens")
def register_end_user_apns_token(
    body: EndUserApnsRegisterIn,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 29.4 stub: native iOS app registers an APNS device
    token. Parallel to POST /me/end-user/push-subscriptions for
    Web Push.

    Upserts by (end_user_id, device_token) — APNS tokens rotate so
    a re-register from the same device updates the existing row
    + clears revoked_at.
    """
    existing = session.scalar(
        select(EndUserApnsToken).where(
            EndUserApnsToken.end_user_id == auth.end_user.id,
            EndUserApnsToken.device_token == body.device_token,
        )
    )
    if existing is not None:
        existing.bundle_id = body.bundle_id
        existing.environment = body.environment
        existing.revoked_at = None
        row = existing
    else:
        row = EndUserApnsToken(
            id=str(uuid.uuid4()),
            end_user_id=auth.end_user.id,
            device_token=body.device_token,
            bundle_id=body.bundle_id,
            environment=body.environment,
        )
        session.add(row)
    session.flush()
    return {
        "id": row.id,
        "device_token": row.device_token,
        "active": row.revoked_at is None,
    }


@app.delete("/me/end-user/apns-tokens")
def revoke_end_user_apns_token(
    body: EndUserApnsUnregisterIn,
    auth: EndUserAuthResult = Depends(get_end_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 29.4 stub: end user disables APNS push on a device
    (sign-out or settings toggle). Soft-revoke; same isolation +
    idempotence shape as the Web Push DELETE."""
    row = session.scalar(
        select(EndUserApnsToken).where(
            EndUserApnsToken.end_user_id == auth.end_user.id,
            EndUserApnsToken.device_token == body.device_token,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "apns_token_not_found"},
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.flush()
    return {"revoked": True, "device_token": row.device_token}


# ---------- Phase 29.2c stub: Sign in with Apple ---------- #


@app.post("/auth/end-user/sign-in-with-apple", status_code=200)
def sign_in_with_apple(
    body: EndUserSignInWithAppleIn,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 29.2c live: exchange an Apple identity token for a
    Lightsei session.

    Verifies the identity token via apple_signin.verify_identity_token,
    keys account lookup by email (claim.email when Apple sent it on
    first sign-in, else body.email forwarded by the iOS app from
    its first-signin cache), upserts an EndUser, creates an
    EndUserSession, returns the same shape as magic-link consume.

    Returns:
      501 siwa_not_configured  → REQUIRE_LIVE not set, no verify
      401 siwa_invalid_token   → JWT signature/claim validation
      422 siwa_missing_email   → no email anywhere (caller bug)
      200 success
    """
    import apple_signin as _siwa

    try:
        claim = _siwa.verify_identity_token(body.identity_token)
    except _siwa.SiwaNotConfiguredError as e:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "siwa_not_configured",
                "message": str(e),
            },
        ) from None
    except _siwa.SiwaInvalidTokenError as e:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "siwa_invalid_token",
                "message": str(e),
            },
        ) from None

    # Apple sends email only on the FIRST sign-in per Apple ID.
    # Subsequent signs-in carry just sub; the iOS app caches the
    # original email + forwards it via body.email so the backend
    # can keep keying off email. If neither, the caller didn't
    # cache properly — fail loud.
    email = (claim.email or body.email or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "siwa_missing_email",
                "message": (
                    "Apple did not send an email + the request did "
                    "not carry one. Re-sign on the device to refresh."
                ),
            },
        )

    now = utcnow()

    existing = session.scalar(
        select(EndUser).where(EndUser.email == email)
    )
    is_new = existing is None
    if existing is None:
        end_user = EndUser(
            id=str(uuid.uuid4()),
            email=email,
            display_name=body.display_name,
            email_verified=claim.email_verified,
            auth_provider="siwa",
            created_at=now,
        )
        session.add(end_user)
        session.flush()
    else:
        end_user = existing
        # If the original account was magic-link only and Apple
        # vouches for the email, mark it verified going forward.
        if claim.email_verified and not end_user.email_verified:
            end_user.email_verified = True

    sess_row, plaintext = _create_end_user_session(session, end_user)

    return {
        "session_token": plaintext,
        "end_user": _serialize_end_user(end_user),
        "is_new_end_user": is_new,
        "session_expires_at": sess_row.expires_at.isoformat(),
    }


# ---------- Phase 17.3: Google OAuth ---------- #


# How long the (state, code_verifier) row in oauth_pending_states is
# valid. Has to cover the user's hop out to Google's consent screen +
# back. 10 minutes is generous; reduce later if abandoned-flow rows
# pile up.
OAUTH_STATE_TTL = timedelta(minutes=10)


@app.get("/auth/google/start")
def google_oauth_start(
    redirect_after: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.3: kick off Google OAuth.

    Returns `{authorization_url, state}` — the dashboard navigates the
    browser to authorization_url. State + PKCE verifier are persisted
    so /auth/google/callback can rebind on return.

    503 if the Google OAuth client isn't configured (env vars not set);
    fail loud rather than redirecting the user into a half-configured
    flow that errors out on Google's end with a less actionable message.

    `redirect_after` is where the dashboard wants the user to land
    post-signin (e.g. the page they tried to reach signed-out). Optional;
    default is `/`.
    """
    import google_oauth as _g

    if not _g.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured on this backend. Set "
                "LIGHTSEI_GOOGLE_CLIENT_ID + LIGHTSEI_GOOGLE_CLIENT_SECRET."
            ),
        )

    now = utcnow()
    verifier, challenge = _g.new_pkce_pair()
    state = _g.new_state()

    session.add(OAuthPendingState(
        state=state,
        code_verifier=verifier,
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + OAUTH_STATE_TTL,
    ))
    session.flush()

    return {
        "authorization_url": _g.build_authorization_url(
            state=state, challenge=challenge,
        ),
        "state": state,
    }


@app.get("/auth/google/callback")
def google_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.3: complete Google OAuth.

    Validates state, exchanges code for tokens, fetches userinfo,
    matches a returning user (by google_user_id exact, fallback to
    verified-email) or creates a fresh user+workspace pair.

    Error paths return 400 with a JSON body the dashboard's callback
    page can render directly. Unknown / expired state → 400 (treats
    the same so we don't leak which one).
    """
    import google_oauth as _g

    if error:
        # Google can hand back ?error=access_denied if the user cancelled
        # on the consent screen. Surface a clean 400; the dashboard
        # callback page renders "Sign-in cancelled" and links back to
        # /login.
        raise HTTPException(
            status_code=400,
            detail={"error": error, "message": "Google sign-in was cancelled or refused."},
        )

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_params", "message": "missing code or state"},
        )

    pending = session.get(OAuthPendingState, state)
    now = utcnow()
    if pending is None or pending.expires_at <= now:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_state",
                "message": "OAuth state is unknown or expired — start the sign-in flow again.",
            },
        )

    # Single-use: delete the pending row before doing the exchange so
    # a parallel callback with the same state can't double-consume.
    verifier = pending.code_verifier
    redirect_after = pending.redirect_after or "/"
    session.delete(pending)
    session.flush()

    try:
        claims = _g.exchange_code_for_userinfo(
            code=code, code_verifier=verifier,
        )
    except _g.GoogleOAuthError as exc:
        # Logged inside the helper; surface a user-friendly 400.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "exchange_failed",
                "message": "Google sign-in didn't complete. Try again.",
                "_debug": str(exc),
            },
        )

    sub = claims["sub"]
    email = claims["email"]
    email_verified = claims["email_verified"]

    # Match priority: google_user_id exact → verified-email → new user.
    user = session.execute(
        select(User).where(User.google_user_id == sub)
    ).scalar_one_or_none()

    if user is None and email_verified:
        # Returning email-known user (e.g. signed up via magic-link or
        # apikey first, now connecting Google). Link the google_user_id
        # so future logins skip the email-match path.
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user is not None:
            user.google_user_id = sub

    is_new_user = user is None

    if user is None:
        # Before creating: if the email is already in use by a user
        # that DIDN'T match by sub OR verified-email (i.e. we got here
        # because Google said the email is unverified and the existing
        # user is a different identity), refuse cleanly instead of
        # crashing on the unique-email constraint. Forces the user to
        # sign in via the path that originally created the account
        # (which is the right user-facing behavior — we don't want
        # an unverified Google email to take over a real account).
        existing_with_email = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if existing_with_email is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "email_already_in_use",
                    "message": (
                        "An account with this email already exists, but "
                        "the email on your Google account isn't verified. "
                        "Sign in with the original method (magic link or "
                        "password), then connect Google from /account."
                    ),
                },
            )

        ws = Workspace(
            id=str(uuid.uuid4()),
            name=_workspace_name_for_signup_email(email),
            created_at=now,
            plan_tier="free",
            free_credits_remaining_usd=Decimal("5.00"),
        )
        session.add(ws)
        session.flush()
        seed_default_validators(session, ws.id, now)

        user = User(
            id=str(uuid.uuid4()),
            email=email,
            # No password — OAuth-only users can't fall back to
            # /auth/login. Same placeholder pattern magic-link uses.
            password_hash="oauth-only:no-password",
            workspace_id=ws.id,
            created_at=now,
            email_verified=email_verified,
            auth_provider="google_oauth",
            google_user_id=sub,
        )
        session.add(user)
        session.flush()
        # Phase 23.2: see the apikey signup site for the same insert.
        session.add(WorkspaceMember(user_id=user.id, workspace_id=ws.id))
    else:
        # Existing user signing in via Google — promote to verified if
        # Google says the email is verified. Doesn't rewrite the
        # original auth_provider.
        if email_verified and not user.email_verified:
            user.email_verified = True

    sess_row, sess_plain = _create_session(session, user)
    ws = session.get(Workspace, user.workspace_id)

    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws) if ws else None,
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
        "is_new_user": is_new_user,
        "redirect_after": redirect_after,
    }


@app.get("/auth/me")
def auth_me(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    ws = session.get(Workspace, auth.workspace_id)
    return {
        "user": _serialize_user(auth.user) if auth.user else None,
        "workspace": _serialize_workspace(ws) if ws else None,
        "credential": "session" if auth.user else "api_key",
    }


def _serialize_session(s: SessionRow, current: bool) -> dict[str, Any]:
    return {
        "id": s.id,
        "created_at": s.created_at.isoformat(),
        "expires_at": s.expires_at.isoformat(),
        "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
        "current": current,
    }


@app.get("/auth/sessions")
def list_sessions(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if auth.user is None:
        raise HTTPException(
            status_code=400, detail="sessions are only visible to a logged-in user"
        )
    rows = session.execute(
        select(SessionRow)
        .where(SessionRow.user_id == auth.user.id)
        .order_by(desc(SessionRow.created_at))
    ).scalars().all()
    current_id = auth.session.id if auth.session else None
    return {
        "sessions": [
            _serialize_session(s, current=s.id == current_id) for s in rows
        ]
    }


@app.delete("/auth/sessions/{session_id}")
def revoke_session(
    session_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if auth.user is None:
        raise HTTPException(status_code=400, detail="must be logged in")
    row = session.get(SessionRow, session_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status_code=404, detail="session not found")
    if auth.session and row.id == auth.session.id:
        raise HTTPException(
            status_code=400, detail="cannot revoke the session used for this request"
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
    session.flush()
    return _serialize_session(row, current=False)


# ---------- Phase 17.4: Stripe billing endpoints ---------- #


@app.post("/workspaces/me/billing/checkout")
def billing_create_checkout(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.4: create a Stripe Checkout session for the workspace.

    Lazy customer creation: most workspaces never upgrade, so we only
    call stripe.Customer.create the first time the user clicks
    "upgrade." Subsequent clicks reuse the stored customer_id.

    Returns `{checkout_url}`. The dashboard navigates the browser to it;
    on success Stripe redirects back to /account?upgrade=success, and
    the dashboard polls /auth/me until plan_tier flips to 'paid' (the
    webhook handler below lands within seconds).
    """
    import stripe_billing as _sb

    if not _sb.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Billing is not configured on this backend. Set "
                "LIGHTSEI_STRIPE_SECRET_KEY + LIGHTSEI_STRIPE_PRICE_ID."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="billing requires a logged-in user (not an API key)",
        )

    ws = session.get(Workspace, auth.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.plan_tier == "paid":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "already_paid",
                "message": "Workspace already has an active subscription. Use /workspaces/me/billing/portal to manage it.",
            },
        )

    try:
        if not ws.stripe_customer_id:
            ws.stripe_customer_id = _sb.create_customer(
                email=auth.user.email, workspace_id=ws.id,
            )
            session.flush()
        result = _sb.create_checkout_session(
            customer_id=ws.stripe_customer_id, workspace_id=ws.id,
        )
    except _sb.StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except _sb.StripeApiError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "stripe_error",
                "message": "Billing is temporarily unavailable. Try again in a moment.",
                "_debug": str(exc),
            },
        )

    return {"checkout_url": result["url"], "session_id": result["id"]}


@app.post("/workspaces/me/billing/portal")
def billing_create_portal(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.4: create a Stripe Customer Portal session.

    Used by /account's "manage subscription" button on paid workspaces.
    400 if the workspace has never been to Checkout (no stripe_customer_id)
    so the dashboard can show "upgrade first" instead of a broken link.
    """
    import stripe_billing as _sb

    if not _sb.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Billing is not configured on this backend. Set "
                "LIGHTSEI_STRIPE_SECRET_KEY + LIGHTSEI_STRIPE_PRICE_ID."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="billing requires a logged-in user (not an API key)",
        )

    ws = session.get(Workspace, auth.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not ws.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_customer",
                "message": "Workspace has no billing history yet. Upgrade first.",
            },
        )

    try:
        result = _sb.create_portal_session(customer_id=ws.stripe_customer_id)
    except _sb.StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except _sb.StripeApiError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "stripe_error",
                "message": "Billing is temporarily unavailable. Try again in a moment.",
                "_debug": str(exc),
            },
        )

    return {"portal_url": result["url"], "session_id": result["id"]}


# Subscription lifecycle events we react to. Any other event types
# Stripe sends are acknowledged with 200 + ignored — we don't want
# Stripe retrying forever for events we genuinely don't care about.
_HANDLED_WEBHOOK_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
}


@app.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Phase 17.4: Stripe webhook handler.

    Verifies the Stripe-Signature header against the configured signing
    secret, then flips plan_tier on the affected workspace based on the
    event type.

    Returns 200 on success (Stripe stops retrying). 400 on bad signature
    or unknown workspace (also stops retries). 5xx is reserved for
    transient infra failures where we WANT Stripe to retry.

    Idempotency: Stripe can deliver the same event twice. We re-derive
    plan_tier from the event's subscription status on every delivery,
    so duplicate delivery is a no-op (writes the same value twice).
    """
    import stripe_billing as _sb

    if not _sb.is_webhook_configured():
        # Shouldn't happen in a properly-configured deployment, but if
        # it does, log loud + return 400 so Stripe doesn't pile up
        # retries against a misconfigured endpoint.
        import logging as _logging
        _logging.getLogger("lightsei.billing").warning(
            "stripe webhook hit but LIGHTSEI_STRIPE_WEBHOOK_SECRET is not set"
        )
        raise HTTPException(
            status_code=400, detail="webhook secret not configured"
        )

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = _sb.construct_webhook_event(
            payload=payload, signature_header=signature,
        )
    except _sb.WebhookSignatureError as exc:
        raise HTTPException(status_code=400, detail=f"bad signature: {exc}")

    event_type = event.get("type") or ""
    if event_type not in _HANDLED_WEBHOOK_EVENTS:
        # Acknowledge + ignore. Common case for events we subscribe to
        # implicitly via the endpoint config (Stripe lets you select
        # specific events, but if the user picks "all events" we don't
        # want to 4xx the ones we don't care about).
        return {"status": "ignored", "type": event_type}

    obj = (event.get("data") or {}).get("object") or {}

    # Pull workspace_id off the event. checkout.session.completed has it
    # in client_reference_id; subscription events carry it in metadata
    # because we copied it onto subscription_data.metadata at checkout
    # creation time.
    workspace_id = (
        obj.get("client_reference_id")
        or (obj.get("metadata") or {}).get("workspace_id")
    )
    if not workspace_id:
        # Fallback: look up by stripe_customer_id. Covers older
        # subscriptions created before we started stamping the metadata.
        customer_id = obj.get("customer")
        if customer_id:
            ws = session.execute(
                select(Workspace).where(Workspace.stripe_customer_id == customer_id)
            ).scalar_one_or_none()
            if ws is not None:
                workspace_id = ws.id

    if not workspace_id:
        import logging as _logging
        _logging.getLogger("lightsei.billing").warning(
            "stripe webhook %s: no workspace_id in event %s",
            event_type, event.get("id"),
        )
        # Return 200 so Stripe stops retrying; the event is for a
        # workspace we don't recognize (likely a test event or a
        # workspace deleted on our side after subscription creation).
        return {"status": "ignored", "reason": "unknown_workspace"}

    ws = session.get(Workspace, workspace_id)
    if ws is None:
        return {"status": "ignored", "reason": "workspace_not_found"}

    if event_type == "checkout.session.completed":
        # Subscription is freshly created. The subscription id is on
        # the checkout session.
        sub_id = obj.get("subscription")
        if sub_id:
            ws.stripe_subscription_id = sub_id
        ws.plan_tier = "paid"
    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        # Set plan_tier based on the subscription's current status.
        # 'active' + 'trialing' are paid; everything else (past_due,
        # unpaid, canceled, incomplete) downgrades to free so the
        # paywall starts firing again until they fix payment.
        status = obj.get("status") or ""
        ws.stripe_subscription_id = obj.get("id") or ws.stripe_subscription_id
        ws.plan_tier = "paid" if status in ("active", "trialing") else "free"
    elif event_type == "customer.subscription.deleted":
        # Subscription cancelled (either by the user via the Portal or
        # by Stripe after exhausted dunning). Drop to free; they keep
        # whatever free credits remain.
        ws.plan_tier = "free"
        ws.stripe_subscription_id = None
    elif event_type == "invoice.payment_failed":
        # First failure: log it but don't change plan_tier yet —
        # subscription.updated with status='past_due' will follow and
        # do the downgrade. This branch is mostly here for telemetry.
        import logging as _logging
        _logging.getLogger("lightsei.billing").warning(
            "stripe webhook: invoice payment failed for workspace %s",
            workspace_id,
        )

    session.flush()
    return {"status": "ok", "type": event_type, "workspace_id": workspace_id}


# ---------- Phase 19.2: Slack OAuth ---------- #


# How long the state row in slack_oauth_pending_states is valid. Has to
# cover the user's hop out to Slack's consent screen + back. 10 minutes
# is generous; reduce later if abandoned-flow rows pile up.
SLACK_OAUTH_STATE_TTL = timedelta(minutes=10)


@app.get("/slack/oauth/start")
def slack_oauth_start(
    redirect_after: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 19.2: kick off the Slack app install flow.

    Returns `{authorization_url, state}` — the dashboard navigates the
    browser to authorization_url. State is persisted to
    slack_oauth_pending_states with the Lightsei workspace id + the
    operator's user id so the callback can rebind to the right
    workspace on return.

    503 if the Slack OAuth client isn't configured (env vars not set);
    fail loud rather than redirecting the user into a half-configured
    flow that errors out on Slack's end.

    `redirect_after` is where the dashboard wants the user to land
    post-install (defaults to /integrations/slack?installed=true in
    the callback).
    """
    import slack_oauth as _so

    if not _so.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Slack OAuth is not configured on this backend. Set "
                "LIGHTSEI_SLACK_CLIENT_ID + LIGHTSEI_SLACK_CLIENT_SECRET."
            ),
        )
    if auth.user is None:
        # Slack install is always operator-driven; can't be initiated
        # by an API-key-only context because we need to track who
        # clicked install on the audit trail.
        raise HTTPException(
            status_code=400,
            detail="Slack install must be initiated by a logged-in user",
        )

    now = utcnow()
    state = _so.new_state()

    session.add(SlackOAuthPendingState(
        state=state,
        lightsei_workspace_id=auth.workspace_id,
        installed_by_user_id=auth.user.id,
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + SLACK_OAUTH_STATE_TTL,
    ))
    session.flush()

    return {
        "authorization_url": _so.build_authorization_url(state=state),
        "state": state,
    }


@app.get("/slack/oauth/callback")
def slack_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Any:
    """Phase 19.2: complete the Slack install.

    Slack redirects the user's browser here after the consent screen.
    We validate the state, exchange the code for a bot token, encrypt
    + persist the bot token in slack_workspaces, then redirect the
    browser to the dashboard's /integrations/slack page.

    Error paths render a small HTML body the user can read and act on,
    not a JSON 4xx, because the user is in a browser tab — this is the
    one Lightsei endpoint that's expected to render directly to a
    person rather than feed a JS fetch.
    """
    import slack_oauth as _so
    import secrets_crypto

    dashboard_base = os.environ.get(
        "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
    ).rstrip("/")

    def _html_error(title: str, message: str) -> Response:
        body = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Slack install — Lightsei</title>"
            "<style>body{font:14px/1.5 -apple-system,sans-serif;"
            "max-width:480px;margin:64px auto;padding:0 16px;color:#111}"
            "h1{font-size:18px;font-weight:600;margin-bottom:8px}"
            "p{color:#555;margin:8px 0}a{color:#4f46e5}</style></head>"
            f"<body><h1>{title}</h1><p>{message}</p>"
            f"<p><a href=\"{dashboard_base}/integrations\">"
            "← back to integrations</a></p></body></html>"
        )
        return Response(content=body, media_type="text/html", status_code=400)

    if error:
        # Slack hands back ?error=access_denied if the user cancelled
        # on the consent screen.
        return _html_error(
            "Slack install was cancelled",
            f"Slack returned an error: {error}. You can try again from "
            "the integrations page.",
        )

    if not code or not state:
        return _html_error(
            "Invalid Slack install link",
            "The Slack install callback was missing required parameters. "
            "Start the install again from the integrations page.",
        )

    pending = session.get(SlackOAuthPendingState, state)
    now = utcnow()
    if pending is None or pending.expires_at <= now:
        return _html_error(
            "Slack install link expired",
            "The install link is no longer valid (expired or already used). "
            "Start a fresh install from the integrations page.",
        )

    workspace_id = pending.lightsei_workspace_id
    installed_by_user_id = pending.installed_by_user_id
    redirect_after = pending.redirect_after
    # Single-use: drop the pending row before doing the exchange so a
    # parallel callback with the same state can't double-consume.
    session.delete(pending)
    session.flush()

    try:
        claims = _so.exchange_code_for_token(code=code)
    except _so.SlackOAuthError as exc:
        return _html_error(
            "Slack install didn't complete",
            f"The token exchange with Slack failed: {exc}. Try installing again.",
        )

    # Encrypt + store. The bot token is xoxb-... — sensitive enough that
    # we never log it; secrets_crypto.encrypt mirrors the workspace-
    # secret encryption path. Store as ASCII bytes of the base64 blob
    # so the LargeBinary column doesn't need a schema change.
    encrypted_blob = secrets_crypto.encrypt(claims["access_token"])
    token_bytes = encrypted_blob.encode("ascii")

    # If this Slack workspace was previously installed (and not revoked),
    # update the existing row in place rather than inserting a new one —
    # the partial-unique index would otherwise reject the new row.
    existing = session.get(SlackWorkspace, claims["team_id"])
    if existing is not None:
        existing.lightsei_workspace_id = workspace_id
        existing.team_name = claims["team_name"]
        existing.bot_token_encrypted = token_bytes
        existing.bot_user_id = claims["bot_user_id"]
        existing.installed_by_user_id = installed_by_user_id
        existing.installed_at = now
        existing.revoked_at = None
    else:
        session.add(SlackWorkspace(
            slack_team_id=claims["team_id"],
            lightsei_workspace_id=workspace_id,
            team_name=claims["team_name"],
            bot_token_encrypted=token_bytes,
            bot_user_id=claims["bot_user_id"],
            installed_by_user_id=installed_by_user_id,
            installed_at=now,
        ))
    session.flush()

    target = redirect_after or f"{dashboard_base}/integrations/slack?installed=true"
    # 303 makes the browser switch from GET to GET (which is what we
    # want — Slack's redirect was a GET; we're handing off to the
    # dashboard which is also GET).
    return RedirectResponse(target, status_code=303)


# ---------- Phase 19.3: Slack events webhook ---------- #


# Event types we route into the generation_jobs queue. Anything else
# is acknowledged with 200 + ignored so Slack stops retrying — the
# events webhook config in 19.7 will request only the events we
# handle, but Slack sometimes sends events we didn't subscribe to
# (e.g. when the app is added to a new channel).
_HANDLED_SLACK_EVENT_TYPES = {
    "app_mention",
}


@app.post("/slack/events")
async def slack_events(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Phase 19.3: Slack events webhook.

    Slack POSTs every subscribed event here (mentions, slash commands,
    membership changes). We:

    1. Echo the URL-verification challenge so Slack accepts the endpoint
       at configuration time.
    2. Verify the X-Slack-Signature HMAC + timestamp window so attackers
       can't forge events.
    3. Check idempotency against slack_events: Slack retries delivery up
       to 3 times within an hour; we treat a duplicate event_id as
       already-handled.
    4. Route app_mention events onto the generation_jobs queue as kind
       'slack_orchestration'. The 19.4 chat orchestrator handler picks
       them up + decides which bot responds.
    5. Acknowledge anything else with 200 + ignored.

    Returns 400 (not 5xx) on bad signature or missing config so Slack
    stops retrying a permanently-broken delivery.
    """
    import slack_events as _se

    payload_bytes = await request.body()

    # ---- Pre-parse for URL verification ---- #
    # Slack pings the endpoint at config time with a JSON body of
    # {type: "url_verification", challenge: "..."}. The signature is
    # included on this request too, but we want to handle the handshake
    # even on a fresh deployment where the signing secret might not
    # yet be in env. So: try to parse first; if it's a URL verification
    # and there's no challenge to verify against, just echo.
    try:
        body_json = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        # Not JSON; reject. Slack only ever sends JSON.
        raise HTTPException(status_code=400, detail="payload is not valid JSON")

    if body_json.get("type") == "url_verification":
        # Echo the challenge so Slack accepts the endpoint. Slack still
        # signs this request, but the URL-verification handshake is
        # what registers the endpoint in the first place, so we don't
        # gate it on signing-secret config.
        challenge = body_json.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(
                status_code=400,
                detail="url_verification missing challenge",
            )
        return {"challenge": challenge}

    # ---- Signature verification ---- #
    if not _se.is_signing_configured():
        import logging as _logging
        _logging.getLogger("lightsei.slack_events").warning(
            "slack events webhook hit but LIGHTSEI_SLACK_SIGNING_SECRET is not set"
        )
        raise HTTPException(
            status_code=400, detail="signing secret not configured"
        )

    try:
        _se.verify_signature(
            body=payload_bytes,
            timestamp_header=request.headers.get("x-slack-request-timestamp"),
            signature_header=request.headers.get("x-slack-signature"),
        )
    except _se.SlackSignatureError as exc:
        raise HTTPException(status_code=400, detail=f"bad signature: {exc}")

    # ---- Idempotency check ---- #
    event = body_json.get("event") or {}
    event_id = body_json.get("event_id")
    slack_team_id = body_json.get("team_id") or body_json.get("api_app_id")
    if not event_id or not slack_team_id:
        # Malformed envelope. Still 200 + ignore so Slack doesn't retry.
        return {"status": "ignored", "reason": "missing_event_id_or_team_id"}

    # Try to insert the idempotency row first. If it's a duplicate,
    # IntegrityError signals we've already handled this; return 200 +
    # no-op so Slack stops retrying.
    from sqlalchemy.exc import IntegrityError
    now = utcnow()
    try:
        session.add(SlackEvent(
            slack_team_id=slack_team_id,
            event_id=event_id,
            kind=event.get("type") or "unknown",
            received_at=now,
        ))
        session.flush()
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate", "event_id": event_id}

    # ---- Event routing ---- #
    event_type = event.get("type") or ""
    if event_type not in _HANDLED_SLACK_EVENT_TYPES:
        # Acknowledge + ignore. The Slack-app config in 19.7 will
        # subscribe only to events we handle, but Slack still sends
        # some events implicitly (membership_changed, etc.).
        return {"status": "ignored", "type": event_type}

    # Look up the Lightsei workspace from the slack_team_id so the
    # orchestrator job runs against the right workspace.
    sw = session.get(SlackWorkspace, slack_team_id)
    if sw is None or sw.revoked_at is not None:
        # Slack workspace isn't connected to a Lightsei workspace
        # (revoked or never installed). Drop the event silently.
        return {"status": "ignored", "reason": "slack_workspace_not_installed"}

    # Queue an orchestration job. The 19.4 handler reads (slack_team_id,
    # channel_id, user_id, text, thread_ts?) from the payload + decides
    # which bot responds.
    from jobs import enqueue_job
    job_id = str(uuid.uuid4())
    enqueue_job(
        session,
        job_id=job_id,
        workspace_id=sw.lightsei_workspace_id,
        kind="slack_orchestration",
        request_payload={
            "slack_team_id": slack_team_id,
            "channel_id": event.get("channel"),
            "user_id": event.get("user"),
            "text": event.get("text") or "",
            "thread_ts": event.get("thread_ts"),
            "ts": event.get("ts"),
            "slack_event_id": event_id,
        },
    )

    return {
        "status": "queued",
        "type": event_type,
        "job_id": job_id,
    }


# ---------- Phase 20.2: Google OAuth (connector install) ---------- #


CONNECTOR_OAUTH_STATE_TTL = timedelta(minutes=10)


@app.get("/connectors/google/start")
def connector_google_oauth_start(
    type: str,
    redirect_after: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 20.2: kick off the Google OAuth install for a connector.

    `type` is the connector name (e.g. `gmail`, `google_calendar`,
    `google_drive`). The scopes Lightsei requests come from the
    connector's `default_scopes` in CONNECTOR_REGISTRY.

    Returns `{authorization_url, state}` — the dashboard navigates the
    browser to authorization_url. State is persisted with the
    Lightsei workspace + operator + connector_type so the callback
    can rebind on return.
    """
    from connectors import get_connector
    from connectors import google_oauth as _gco

    spec = get_connector(type)
    if spec is None or spec.oauth_provider != "google":
        raise HTTPException(
            status_code=404,
            detail=f"unknown google connector {type!r}",
        )
    if not _gco.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured on this backend. Set "
                "LIGHTSEI_GOOGLE_CLIENT_ID + LIGHTSEI_GOOGLE_CLIENT_SECRET."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="connector install must be initiated by a logged-in user",
        )

    now = utcnow()
    verifier, challenge = _gco.new_pkce_pair()
    state = _gco.new_state()

    session.add(ConnectorOAuthPendingState(
        state=state,
        workspace_id=auth.workspace_id,
        installed_by_user_id=auth.user.id,
        connector_type=type,
        code_verifier=verifier,
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + CONNECTOR_OAUTH_STATE_TTL,
    ))
    session.flush()

    return {
        "authorization_url": _gco.build_authorization_url(
            state=state,
            challenge=challenge,
            scopes=list(spec.default_scopes),
        ),
        "state": state,
    }


@app.get("/connectors/google/callback")
def connector_google_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Any:
    """Phase 20.2: complete the connector install.

    Validates state, single-use'd (drops the pending row before
    exchange), trades the code for tokens, encrypts the {access_token,
    refresh_token, expires_at} blob, persists a ConnectorInstallation
    row (or updates an existing one in place for re-install), redirects
    the browser back to the dashboard.

    Renders HTML 400s on error paths so a browser tab gets a useful
    page rather than JSON (same pattern as 19.2's Slack callback).
    """
    import json as _json
    import secrets_crypto
    from connectors import get_connector
    from connectors import google_oauth as _gco

    dashboard_base = os.environ.get(
        "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
    ).rstrip("/")

    def _html_error(title: str, message: str) -> Response:
        body = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Connector install — Lightsei</title>"
            "<style>body{font:14px/1.5 -apple-system,sans-serif;"
            "max-width:480px;margin:64px auto;padding:0 16px;color:#111}"
            "h1{font-size:18px;font-weight:600;margin-bottom:8px}"
            "p{color:#555;margin:8px 0}a{color:#4f46e5}</style></head>"
            f"<body><h1>{title}</h1><p>{message}</p>"
            f"<p><a href=\"{dashboard_base}/integrations\">"
            "← back to integrations</a></p></body></html>"
        )
        return Response(content=body, media_type="text/html", status_code=400)

    if error:
        return _html_error(
            "Connector install cancelled",
            f"Google returned an error: {error}. You can try again from "
            "the integrations page.",
        )

    if not code or not state:
        return _html_error(
            "Invalid install link",
            "The Google install callback was missing required parameters. "
            "Start the install again from the integrations page.",
        )

    pending = session.get(ConnectorOAuthPendingState, state)
    now = utcnow()
    if pending is None or pending.expires_at <= now:
        return _html_error(
            "Install link expired",
            "The install link is no longer valid (expired or already used). "
            "Start a fresh install from the integrations page.",
        )

    workspace_id = pending.workspace_id
    installed_by_user_id = pending.installed_by_user_id
    connector_type = pending.connector_type
    verifier = pending.code_verifier
    redirect_after = pending.redirect_after
    # Single-use: drop the pending row before exchange so a parallel
    # callback with the same state can't double-consume.
    session.delete(pending)
    session.flush()

    spec = get_connector(connector_type)
    if spec is None or spec.oauth_provider != "google":
        return _html_error(
            "Unknown connector",
            f"The connector {connector_type!r} is no longer recognized.",
        )

    try:
        tokens = _gco.exchange_code_for_tokens(
            code=code, code_verifier=verifier,
        )
    except _gco.GoogleConnectorOAuthError as exc:
        return _html_error(
            "Install didn't complete",
            f"The token exchange with Google failed: {exc}. Try again "
            "from the integrations page.",
        )

    # Encrypt the token blob. We serialize a JSON dict so the refresh
    # path (20.6) can read+write the same shape, and `expires_at` is
    # carried as an ISO timestamp rather than seconds-from-now so we
    # don't have to plumb the install timestamp through every refresh.
    expires_at = None
    if tokens.get("expires_in") is not None:
        expires_at = (now + timedelta(seconds=int(tokens["expires_in"]))).isoformat()
    token_blob = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": expires_at,
    }
    encrypted = secrets_crypto.encrypt(_json.dumps(token_blob)).encode("ascii")

    # Granted scopes are a subset of what we asked for (user can
    # decline). Store what they actually granted.
    granted_scopes = (tokens.get("scope") or "").split() if tokens.get("scope") else []
    email = tokens.get("email")

    # Reinstall path: existing active install for (workspace, type)
    # gets updated in place so the partial-unique index doesn't fire.
    existing = session.execute(
        select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == workspace_id,
            ConnectorInstallation.connector_type == connector_type,
            ConnectorInstallation.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing.encrypted_tokens = encrypted
        existing.scopes = granted_scopes
        existing.installed_by_user_id = installed_by_user_id
        existing.external_account_email = email or existing.external_account_email
        existing.installed_at = now
    else:
        session.add(ConnectorInstallation(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            connector_type=connector_type,
            encrypted_tokens=encrypted,
            scopes=granted_scopes,
            installed_by_user_id=installed_by_user_id,
            external_account_email=email,
            installed_at=now,
        ))
    session.flush()

    target = redirect_after or f"{dashboard_base}/integrations?installed={connector_type}"
    return RedirectResponse(target, status_code=303)


# ---------- Phase 20.8: operator-facing connector list + revoke ---------- #


def _serialize_connector_install(row: ConnectorInstallation) -> dict[str, Any]:
    """Surface only the non-secret bits of an install — tokens stay
    encrypted in storage, never returned over the wire."""
    return {
        "id": row.id,
        "external_account_email": row.external_account_email,
        "scopes": list(row.scopes or []),
        "installed_at": row.installed_at.isoformat(),
        "installed_by_user_id": row.installed_by_user_id,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


@app.get("/workspaces/me/connectors")
def list_workspace_connectors(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 20.8: list every connector in the registry + its install
    state for this workspace.

    Returns one entry per registry connector (Gmail / Calendar / Drive
    at v1) so the dashboard's card grid can render the not-installed
    state alongside installed ones. `install` is null when no active
    row exists for the workspace; otherwise it carries the
    `external_account_email`, granted `scopes`, `installed_at`, and
    `installed_by_user_id` so the card can show 'Connected as X' and
    'Granted Y scopes'.

    Tokens never appear in the response — they stay encrypted in
    `connector_installations.encrypted_tokens` and only the
    bot-callable endpoint (20.6) decrypts them.
    """
    from connectors import list_connectors

    # Pull all active installs for this workspace in one query so we
    # don't N+1 across the registry.
    active_installs = session.execute(
        select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == auth.workspace_id,
            ConnectorInstallation.revoked_at.is_(None),
        )
    ).scalars().all()
    by_type: dict[str, ConnectorInstallation] = {
        i.connector_type: i for i in active_installs
    }

    connectors_out = []
    for spec in list_connectors():
        install = by_type.get(spec.name)
        connectors_out.append({
            "type": spec.name,
            "display_label": spec.display_label,
            "oauth_provider": spec.oauth_provider,
            "default_scopes": list(spec.default_scopes),
            "declared_zones": sorted(spec.declared_zones),
            "summary": spec.summary,
            "install": _serialize_connector_install(install) if install else None,
        })

    return {"connectors": connectors_out}


@app.delete("/workspaces/me/connectors/{connector_type}")
def revoke_workspace_connector(
    connector_type: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 20.8: revoke an active connector install.

    Sets `revoked_at` on the install row + best-effort calls Google's
    `/revoke` endpoint to invalidate the refresh token upstream. The
    row stays in the DB for audit; the partial-unique index from 20.1
    lets the same connector re-install after this without manual
    cleanup. Same revoke pattern as the Slack disconnect path in 19.6.

    404 if no active install exists for this workspace.
    """
    import json as _json

    import secrets_crypto
    from connectors import get_connector

    spec = get_connector(connector_type)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown connector {connector_type!r}",
        )

    install = session.execute(
        select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == auth.workspace_id,
            ConnectorInstallation.connector_type == connector_type,
            ConnectorInstallation.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if install is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active {connector_type!r} install in this workspace",
        )

    now = utcnow()
    install.revoked_at = now
    session.flush()

    # Best-effort upstream revoke. Google's /revoke is the canonical
    # path for both Gmail + Calendar + Drive (all share the OAuth
    # tokens). Failure here doesn't roll back the local revoke —
    # the user-facing intent is "stop using this", local revocation
    # is what protects subsequent bot calls.
    if spec.oauth_provider == "google":
        import logging as _logging
        _log = _logging.getLogger("lightsei.connectors")
        try:
            blob = _json.loads(secrets_crypto.decrypt(bytes(install.encrypted_tokens)))
            refresh_token = blob.get("refresh_token")
            if refresh_token:
                import httpx as _httpx
                r = _httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={"token": refresh_token},
                    timeout=5.0,
                )
                if r.status_code >= 400:
                    _log.warning(
                        "google /revoke not ok for %s install %s: %s %s",
                        connector_type, install.id,
                        r.status_code, r.text[:200],
                    )
        except Exception as e:
            _log.warning(
                "google /revoke upstream call failed for %s install %s: %s",
                connector_type, install.id, e,
            )

    return {
        "status": "revoked",
        "connector_type": connector_type,
        "revoked_at": now.isoformat(),
    }


# ---------- Phase 19.6: operator-facing Slack config endpoints ---------- #


def _serialize_slack_workspace(sw: SlackWorkspace) -> dict[str, Any]:
    """Surface the bits the dashboard's /integrations/slack page needs.
    Bot token is NEVER returned — that stays encrypted in storage."""
    return {
        "slack_team_id": sw.slack_team_id,
        "team_name": sw.team_name,
        "bot_user_id": sw.bot_user_id,
        "installed_at": sw.installed_at.isoformat(),
        "installed_by_user_id": sw.installed_by_user_id,
        "revoked_at": sw.revoked_at.isoformat() if sw.revoked_at else None,
    }


def _serialize_slack_channel(ch: SlackChannel) -> dict[str, Any]:
    return {
        "slack_team_id": ch.slack_team_id,
        "channel_id": ch.channel_id,
        "channel_name": ch.channel_name,
        "sensitivity_level": ch.sensitivity_level,
        "opted_in": ch.opted_in,
        "created_at": ch.created_at.isoformat(),
        "updated_at": ch.updated_at.isoformat(),
    }


@app.get("/workspaces/me/slack/workspaces")
def list_slack_workspaces(
    include_revoked: bool = False,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List Slack workspaces connected to this Lightsei workspace.

    By default returns only active installs. Set `include_revoked=true`
    to see historical installs (useful for an audit log surface).
    """
    q = select(SlackWorkspace).where(
        SlackWorkspace.lightsei_workspace_id == auth.workspace_id
    )
    if not include_revoked:
        q = q.where(SlackWorkspace.revoked_at.is_(None))
    q = q.order_by(SlackWorkspace.installed_at.desc())
    rows = session.execute(q).scalars().all()
    return {"workspaces": [_serialize_slack_workspace(r) for r in rows]}


@app.get("/workspaces/me/slack/channels")
def list_slack_channels(
    slack_team_id: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List channels the Lightsei bot has been seen in.

    Filter by `slack_team_id` when the dashboard's already grouped by
    workspace; otherwise returns all channels across all installs
    for this Lightsei workspace. Ordered by (opted_in DESC, channel_name)
    so opted-in channels surface first — they're the actionable ones.
    """
    q = select(SlackChannel).where(
        SlackChannel.lightsei_workspace_id == auth.workspace_id
    )
    if slack_team_id:
        q = q.where(SlackChannel.slack_team_id == slack_team_id)
    q = q.order_by(SlackChannel.opted_in.desc(), SlackChannel.channel_name)
    rows = session.execute(q).scalars().all()
    return {"channels": [_serialize_slack_channel(r) for r in rows]}


class SlackChannelPatchIn(BaseModel):
    """Both fields optional — operator can flip either independently."""
    sensitivity_level: Optional[str] = Field(default=None, max_length=16)
    opted_in: Optional[bool] = None


@app.patch("/workspaces/me/slack/channels/{slack_team_id}/{channel_id}")
def patch_slack_channel(
    slack_team_id: str,
    channel_id: str,
    body: SlackChannelPatchIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Set the channel's `sensitivity_level` and/or `opted_in`. The
    chat orchestrator (Phase 19.4) reads these on every event.

    404 if the channel doesn't belong to this Lightsei workspace.
    422 if sensitivity_level isn't one of the four valid values.
    """
    from models import is_valid_sensitivity_level

    channel = session.get(SlackChannel, (slack_team_id, channel_id))
    if channel is None or channel.lightsei_workspace_id != auth.workspace_id:
        # Treat cross-workspace as 404 so the existence of a channel
        # in another tenant doesn't leak through the error code.
        raise HTTPException(
            status_code=404,
            detail=f"channel {slack_team_id!r}/{channel_id!r} not found",
        )

    if body.sensitivity_level is not None:
        if not is_valid_sensitivity_level(body.sensitivity_level):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"sensitivity_level {body.sensitivity_level!r} not in "
                    "{public, internal, sensitive, pii}"
                ),
            )
        channel.sensitivity_level = body.sensitivity_level

    if body.opted_in is not None:
        channel.opted_in = body.opted_in

    channel.updated_at = utcnow()
    session.flush()
    return _serialize_slack_channel(channel)


@app.delete("/workspaces/me/slack/workspaces/{slack_team_id}")
def revoke_slack_workspace(
    slack_team_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Revoke a Slack install.

    Sets `revoked_at` on the slack_workspaces row + best-effort calls
    Slack's `auth.revoke` to invalidate the stored bot token upstream.
    The row stays in the DB for audit; the partial-unique index from
    19.1 lets the same Slack workspace re-install after this without
    a manual cleanup.

    404 if the workspace install doesn't belong to this Lightsei
    workspace.
    """
    sw = session.get(SlackWorkspace, slack_team_id)
    if sw is None or sw.lightsei_workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=404,
            detail=f"slack workspace {slack_team_id!r} not found",
        )

    now = utcnow()
    if sw.revoked_at is None:
        sw.revoked_at = now
        session.flush()
        # Best-effort upstream revoke. Slack returns ok:true on success;
        # we don't fail the local revoke if Slack rejects (the token
        # might already be invalidated; the local revoke is what matters
        # for routing). Logged for ops.
        try:
            import secrets_crypto
            token = secrets_crypto.decrypt(sw.bot_token_encrypted.decode("ascii"))
            httpx_post = __import__("httpx").post
            r = httpx_post(
                "https://slack.com/api/auth.revoke",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            if r.status_code >= 400 or not r.json().get("ok"):
                import logging as _logging
                _logging.getLogger("lightsei.slack").warning(
                    "auth.revoke not ok for %s: %s %s",
                    slack_team_id, r.status_code, r.text[:200],
                )
        except Exception as e:
            import logging as _logging
            _logging.getLogger("lightsei.slack").warning(
                "auth.revoke upstream call failed for %s: %s",
                slack_team_id, e,
            )

    return _serialize_slack_workspace(sw)


# ---------- Phase 19.5: agent-side Slack response endpoint ---------- #


class SlackRespondIn(BaseModel):
    """Input to `POST /slack/respond` (Phase 19.5).

    Called from bot code via `lightsei.post_slack(channel, text, ...)`.
    `source_agent` identifies the bot making the call so the capability
    gate can check its allow-list."""
    source_agent: str = Field(min_length=1, max_length=128)
    channel: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=40_000)
    thread_ts: Optional[str] = Field(default=None, max_length=64)


@app.post("/slack/respond")
def slack_respond(
    body: SlackRespondIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 19.5: agent-side helper for posting a message to Slack.

    Capability-gated on `slack:respond`. The agent's bot token is
    never exposed to bot code — the backend resolves the workspace's
    Slack install, decrypts the stored bot token, and calls
    chat.postMessage on the agent's behalf.

    Returns the Slack response on success. 4xx surfaces include:
    - 403 cross_zone_blocked-shape detail when the agent doesn't have
      the slack:respond capability granted.
    - 400 when the workspace has no active Slack install (the
      operator hasn't connected Slack yet).
    - 502 with a generic "Slack post failed" when Slack rejects the
      message (channel not found, bot not in channel, etc.).
    """
    import slack_client

    # Capability gate. Same shape as the cross-zone block surface from
    # /agents/{name}/commands so SDK code can map both 403s to the
    # typed LightseiCapabilityError without parsing free-form strings.
    agent = session.get(Agent, (workspace_id, body.source_agent))
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent {body.source_agent!r} not found in this workspace",
        )
    if "slack:respond" not in (agent.capabilities or []):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "capability_missing",
                "capability": "slack:respond",
                "agent_name": body.source_agent,
                "granted": list(agent.capabilities or []),
                "message": (
                    f"agent {body.source_agent!r} does not have the "
                    "'slack:respond' capability. Add it via "
                    "PATCH /agents/{name}/capabilities or set the "
                    "Compliance preset's internal / public hint."
                ),
            },
        )

    # Find the active Slack install for this Lightsei workspace. There
    # can be at most one non-revoked install (partial-unique index).
    active_install = session.execute(
        select(SlackWorkspace).where(
            SlackWorkspace.lightsei_workspace_id == workspace_id,
            SlackWorkspace.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if active_install is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_slack_install",
                "message": (
                    "No active Slack install for this workspace. Connect "
                    "Slack from /integrations/slack first."
                ),
            },
        )

    try:
        result = slack_client.post_message(
            session=session,
            slack_team_id=active_install.slack_team_id,
            channel=body.channel,
            text=body.text,
            thread_ts=body.thread_ts,
        )
    except slack_client.SlackClientError as exc:
        # Slack rejected the post (bot not in channel, channel_not_found,
        # etc.) OR the token is borked. Map to 502 with a clean message;
        # the raw error goes into _debug for ops.
        raise HTTPException(
            status_code=502,
            detail={
                "error": "slack_post_failed",
                "message": "Slack post failed — see _debug for the upstream error.",
                "_debug": str(exc),
            },
        )

    # Return the bits a bot might care about (ts for threading, channel
    # echo for paranoia). Drop everything else from Slack's response
    # to keep the surface stable across Slack API changes.
    return {
        "ok": True,
        "ts": result.get("ts"),
        "channel": result.get("channel"),
    }


# ---------- Phase 20.6: bot-callable connector endpoint ---------- #


class ConnectorInvokeIn(BaseModel):
    """Input to `POST /connectors/{type}/{tool}` (Phase 20.6).

    Called from bot code via `lightsei.gmail.send_email(...)` and
    friends (Phase 20.7 SDK helpers wrap this endpoint). `payload` is
    the tool's input dict; its shape is validated by the connector's
    own MANIFEST input_schema at the per-tool function level."""
    source_agent: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


def invoke_connector_tool(
    session: Session,
    *,
    workspace_id: str,
    connector_type: str,
    tool_name: str,
    payload: dict[str, Any],
    source_agent: str,
) -> Any:
    """Phase 20.6 / 32.12: invoke an installed connector tool, server-side.

    The single implementation of the connector gates + dispatch. Two
    callers: the HTTP endpoint `invoke_connector` (bot-facing) and the
    feeder (Phase 32.12, polling Gmail for the Inbox assistant). Keeping
    one implementation means the capability + trust-zone + install checks
    — the wedge invariant — can't drift between the two paths.

    Returns the raw connector result on success. Raises HTTPException on
    any gate or dispatch failure (the endpoint lets it propagate; the
    feeder catches it and skips that workspace gracefully).

    Original bot-callable surface contract:

    Pipeline (and the rejection shape at each step):

    1. 404 `unknown_connector` if `connector_type` isn't in the
       registry.
    2. 404 if `source_agent` isn't an agent in this workspace.
    3. 403 `capability_missing` if the agent's `capabilities` list
       doesn't include `connector:{type}`. Same shape as
       `/slack/respond`'s 403 so SDK code can map both to
       `LightseiCapabilityError` without parsing strings.
    4. 403 `connector_zone_mismatch` if the agent's
       `sensitivity_level` isn't in the connector spec's
       `declared_zones`. The wedge invariant — e.g. a public-zoned
       bot can never touch Gmail because Gmail's declared_zones
       excludes public.
    5. 400 `connector_not_installed` if the workspace has no active
       install for `connector_type`. Operator hasn't run the
       `/connectors/google/start` flow yet.
    6. Decrypt the stored token blob, dispatch INVOKE.
    7. On `ConnectorAuthExpired` (401 from upstream): refresh the
       access_token via the connector's OAuth helper, persist the new
       encrypted blob, retry the INVOKE once. A second 401 surfaces
       as 502 `connector_auth_failed` — the install needs operator
       attention (refresh_token revoked, scopes changed upstream).
    8. On `ConnectorCallError`: 502 `connector_call_failed` with the
       upstream status in `_debug`.
    9. Record the call on `runs` (+ a single
       `connector_call_completed` event row) so the dashboard can
       answer "what is this bot actually doing in prod?" without an
       Anthropic-cost rollup needing to exist.
    """
    import json as _json
    from types import SimpleNamespace

    # Shim so the long dispatch body below keeps referencing body.payload
    # / body.source_agent unchanged after this was extracted from the HTTP
    # endpoint (now a thin wrapper). Minimizes the diff on a security-
    # sensitive path: the gate logic is byte-identical to before.
    body = SimpleNamespace(payload=payload, source_agent=source_agent)

    import secrets_crypto
    from connectors import (
        CONNECTOR_REGISTRY,
        ConnectorAuthExpired,
        ConnectorCallError,
    )
    from connectors import google_oauth as _gco

    # 1. Registry lookup.
    spec = CONNECTOR_REGISTRY.get(connector_type)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_connector",
                "connector_type": connector_type,
                "message": (
                    f"no connector registered with type "
                    f"{connector_type!r}. Known: "
                    f"{sorted(CONNECTOR_REGISTRY.keys())}."
                ),
            },
        )

    # 2. Agent lookup.
    agent = session.get(Agent, (workspace_id, body.source_agent))
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent {body.source_agent!r} not found in this workspace",
        )

    # 3. Capability gate — same shape as /slack/respond's 403.
    required_capability = f"connector:{connector_type}"
    if required_capability not in (agent.capabilities or []):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "capability_missing",
                "capability": required_capability,
                "agent_name": body.source_agent,
                "granted": list(agent.capabilities or []),
                "message": (
                    f"agent {body.source_agent!r} does not have the "
                    f"{required_capability!r} capability. Add it via "
                    "PATCH /agents/{name}/capabilities."
                ),
            },
        )

    # 4. Zone gate. declared_zones is a frozenset of sensitivity-level
    # strings. A mismatch means the connector flat-out refuses bots
    # at this trust zone (the wedge invariant).
    if agent.sensitivity_level not in spec.declared_zones:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "connector_zone_mismatch",
                "connector_type": connector_type,
                "agent_name": body.source_agent,
                "agent_sensitivity_level": agent.sensitivity_level,
                "declared_zones": sorted(spec.declared_zones),
                "message": (
                    f"connector {connector_type!r} refuses calls from "
                    f"{agent.sensitivity_level!r}-zoned bots. Declared "
                    f"zones: {sorted(spec.declared_zones)}."
                ),
            },
        )

    # 5. Install lookup. Partial-unique index guarantees at most one
    # non-revoked install per (workspace, type).
    install = session.execute(
        select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == workspace_id,
            ConnectorInstallation.connector_type == connector_type,
            ConnectorInstallation.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if install is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "connector_not_installed",
                "connector_type": connector_type,
                "message": (
                    f"no active {connector_type!r} install for this "
                    "workspace. Connect it from /integrations first."
                ),
            },
        )

    # Decrypt the {access_token, refresh_token, expires_at} blob.
    try:
        token_blob = _json.loads(
            secrets_crypto.decrypt(bytes(install.encrypted_tokens))
        )
    except Exception:
        # Crypto failure — install is unrecoverable from here.
        # Surface as 500 since it indicates corruption / misconfig,
        # not a bot-correctable error.
        import logging as _logging
        _logging.getLogger("lightsei.connectors").exception(
            "connector %s install %s token decrypt failed",
            connector_type, install.id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "connector_token_decrypt_failed",
                "connector_type": connector_type,
                "message": (
                    "stored connector tokens could not be decrypted. "
                    "Reinstall the connector from /integrations."
                ),
            },
        )

    access_token = token_blob.get("access_token")
    refresh_token = token_blob.get("refresh_token")

    # 6 + 7. Dispatch, with a single refresh-then-retry on auth-expired.
    now = utcnow()
    invoke_error: Optional[str] = None
    upstream_status: Optional[int] = None
    try:
        try:
            result = spec.invoke(
                tool_name=tool_name,
                payload=body.payload,
                access_token=access_token,
            )
        except ConnectorAuthExpired:
            # Try one refresh + retry. If we have no refresh_token
            # (shouldn't happen — the install path requires it) or the
            # refresh itself fails, the install is dead.
            if not refresh_token:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "connector_auth_failed",
                        "connector_type": connector_type,
                        "message": (
                            "access token expired and no refresh "
                            "token on file. Reinstall the connector."
                        ),
                    },
                )
            try:
                refreshed = _gco.refresh_access_token(
                    refresh_token=refresh_token,
                )
            except _gco.GoogleConnectorOAuthError as exc:
                # invalid_grant etc. — install dead.
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "connector_auth_failed",
                        "connector_type": connector_type,
                        "message": (
                            "token refresh failed; reinstall the "
                            "connector from /integrations."
                        ),
                        "_debug": str(exc),
                    },
                )

            new_access = refreshed["access_token"]
            new_refresh = refreshed.get("refresh_token") or refresh_token
            new_expires_at = None
            if refreshed.get("expires_in") is not None:
                new_expires_at = (
                    now + timedelta(seconds=int(refreshed["expires_in"]))
                ).isoformat()
            new_blob = {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_at": new_expires_at,
            }
            install.encrypted_tokens = secrets_crypto.encrypt(
                _json.dumps(new_blob)
            ).encode("ascii")
            session.flush()

            # Retry once with the fresh access_token. A second 401
            # here means the upstream API is rejecting a fresh token
            # — install is dead from the user's side.
            try:
                result = spec.invoke(
                    tool_name=tool_name,
                    payload=body.payload,
                    access_token=new_access,
                )
            except ConnectorAuthExpired:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "connector_auth_failed",
                        "connector_type": connector_type,
                        "message": (
                            "upstream rejected a freshly-refreshed "
                            "token. Reinstall the connector."
                        ),
                    },
                )
    except ConnectorCallError as exc:
        upstream_status = exc.upstream_status
        invoke_error = str(exc)
        _record_connector_call(
            session,
            workspace_id=workspace_id,
            agent=agent,
            connector_type=connector_type,
            tool_name=tool_name,
            now=now,
            ok=False,
            error=invoke_error,
            upstream_status=upstream_status,
        )
        # Commit the recording before raising — get_session rolls
        # back on exception, which would lose the failure row. Any
        # other pending work (the refreshed encrypted_tokens blob, if
        # we got that far) is also worth persisting.
        session.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "error": "connector_call_failed",
                "connector_type": connector_type,
                "tool_name": tool_name,
                "message": (
                    f"upstream {connector_type} API call failed — see "
                    "_debug for details."
                ),
                "_debug": {
                    "upstream_status": upstream_status,
                    "error": invoke_error,
                },
            },
        )

    # 9. Record the successful call.
    _record_connector_call(
        session,
        workspace_id=workspace_id,
        agent=agent,
        connector_type=connector_type,
        tool_name=tool_name,
        now=now,
        ok=True,
        error=None,
        upstream_status=None,
    )

    return result


@app.post("/connectors/{connector_type}/{tool_name}")
def invoke_connector(
    connector_type: str,
    tool_name: str,
    body: ConnectorInvokeIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 20.6 HTTP surface — thin wrapper over invoke_connector_tool.

    The gates + dispatch live in the helper so the feeder can reach
    connectors server-side through the SAME wedge checks. Any HTTPException
    the helper raises propagates as this endpoint's response unchanged.
    """
    result = invoke_connector_tool(
        session,
        workspace_id=workspace_id,
        connector_type=connector_type,
        tool_name=tool_name,
        payload=body.payload,
        source_agent=body.source_agent,
    )
    return {"ok": True, "result": result}


def _record_connector_call(
    session: Session,
    *,
    workspace_id: str,
    agent: "Agent",
    connector_type: str,
    tool_name: str,
    now: datetime,
    ok: bool,
    error: Optional[str],
    upstream_status: Optional[int],
) -> None:
    """Drop a Run + a single `connector_call_completed` event row so
    the dashboard's per-agent activity views (Phase 11.4 + later) can
    count connector calls alongside Anthropic Runs. cost_usd is zero
    — connector calls don't burn LLM tokens — but `sensitivity_level`
    is snapshotted from the agent so zone-rollup queries stay
    historically correct even if the agent's level changes later.
    """
    run_id = str(uuid.uuid4())
    session.add(Run(
        id=run_id,
        workspace_id=workspace_id,
        agent_name=agent.name,
        started_at=now,
        ended_at=now,
        sensitivity_level=agent.sensitivity_level,
    ))
    session.flush()
    event_payload: dict[str, Any] = {
        "connector_type": connector_type,
        "tool_name": tool_name,
        "ok": ok,
    }
    if error is not None:
        event_payload["error"] = error
    if upstream_status is not None:
        event_payload["upstream_status"] = upstream_status
    session.add(Event(
        workspace_id=workspace_id,
        run_id=run_id,
        agent_name=agent.name,
        kind="connector_call_completed" if ok else "connector_call_failed",
        payload=event_payload,
        timestamp=now,
    ))
    session.flush()


def _serialize_command(c: Command) -> dict[str, Any]:
    return {
        "id": c.id,
        "agent_name": c.agent_name,
        "kind": c.kind,
        "payload": c.payload or {},
        "status": c.status,
        "result": c.result,
        "error": c.error,
        "created_at": c.created_at.isoformat(),
        "claimed_at": c.claimed_at.isoformat() if c.claimed_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "expires_at": c.expires_at.isoformat(),
        # Phase 11.2 dispatch chain fields.
        "source_agent": c.source_agent,
        "dispatch_chain_id": c.dispatch_chain_id,
        "dispatch_depth": c.dispatch_depth,
        "approval_state": c.approval_state,
        "approved_by_user_id": c.approved_by_user_id,
        "approved_at": c.approved_at.isoformat() if c.approved_at else None,
    }


def _resolve_auto_approval(
    session: Session,
    workspace_id: str,
    source_agent: Optional[str],
    target_agent: str,
    command_kind: str,
) -> str:
    """Lookup the approval mode for a (source, target, kind) tuple.

    Precedence:
        1. Exact match on all three.
        2. Wildcard source ('*' source + exact target + exact kind).
        3. Wildcard kind (exact source + exact target + '*' kind).
        4. No match — return 'pending', the default human-in-the-loop.

    `mode='auto_approve'` returns 'auto_approved'; 'require_human'
    returns 'pending' (explicit deny path so a wildcard can be
    overridden by a more specific require_human entry).

    When source_agent is None (user-initiated enqueue from the
    dashboard, or off-platform integration like /webhooks/github),
    skip rule matching entirely — the command goes straight to
    'auto_approved' since the user is already trusted.
    """
    if source_agent is None:
        return "auto_approved"
    candidates = [
        (source_agent, target_agent, command_kind),
        ("*", target_agent, command_kind),
        (source_agent, target_agent, "*"),
    ]
    for src, tgt, kind in candidates:
        rule = session.execute(
            select(CommandAutoApprovalRule).where(
                CommandAutoApprovalRule.workspace_id == workspace_id,
                CommandAutoApprovalRule.source_agent == src,
                CommandAutoApprovalRule.target_agent == tgt,
                CommandAutoApprovalRule.command_kind == kind,
            )
        ).scalar_one_or_none()
        if rule is None:
            continue
        return "auto_approved" if rule.mode == "auto_approve" else "pending"
    return "pending"


@app.post("/agents/{agent_name}/commands")
def enqueue_command(
    agent_name: str,
    body: CommandEnqueueIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enqueue a command for `agent_name`.

    Phase 11.2 changes the shape: `dispatch_chain_id` and
    `source_agent` on the body are honored; depth + per-day caps are
    enforced when source_agent is set; auto_approval rules pre-flip
    the approval gate where applicable.
    """
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)

    parent_depth = -1  # depth = parent_depth + 1, so root commands get 0
    chain_id = body.dispatch_chain_id

    # When source_agent is set, this is an agent-driven dispatch and
    # we enforce the depth + per-day caps on the source agent.
    if body.source_agent:
        # Auto-register the source agent on first dispatch — same
        # pattern as ensure_agent above for the target. Means a new
        # agent can dispatch its first command without a separate
        # "register me" round-trip.
        ensure_agent(session, workspace_id, body.source_agent, now)
        source = session.get(Agent, (workspace_id, body.source_agent))
        if source is None:
            # ensure_agent should have created it; this is paranoia.
            raise HTTPException(
                status_code=400,
                detail=f"unknown source_agent {body.source_agent!r}",
            )
        # Look up the parent command in this chain (most recent for
        # this chain id) so we can compute depth = parent.depth + 1.
        if chain_id:
            parent = session.execute(
                select(Command)
                .where(
                    Command.workspace_id == workspace_id,
                    Command.dispatch_chain_id == chain_id,
                )
                .order_by(desc(Command.dispatch_depth))
                .limit(1)
            ).scalar_one_or_none()
            if parent is not None:
                parent_depth = parent.dispatch_depth
        depth = parent_depth + 1
        if depth >= source.max_dispatch_depth:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"dispatch chain depth {depth} would exceed "
                    f"{body.source_agent}'s max_dispatch_depth = "
                    f"{source.max_dispatch_depth}"
                ),
            )
        # Per-day rate cap: count commands the source has dispatched
        # in the last 24h. Filtered by source_agent so an agent's own
        # incoming commands don't count against its outgoing budget.
        cutoff = now - timedelta(hours=24)
        recent = session.execute(
            text(
                """
                SELECT COUNT(*) AS n FROM commands
                WHERE workspace_id = :wsid
                  AND source_agent = :src
                  AND created_at >= :cutoff
                """
            ),
            {"wsid": workspace_id, "src": body.source_agent, "cutoff": cutoff},
        ).first()
        if recent and (recent.n or 0) >= source.max_dispatch_per_day:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.source_agent} has dispatched {recent.n} "
                    f"commands in the last 24h, exceeding its "
                    f"max_dispatch_per_day = {source.max_dispatch_per_day}"
                ),
            )
        # Phase 16.4: cross-zone dispatch enforcement. The load-bearing
        # piece. Same sensitivity level always allowed; different
        # levels refused unless the source agent has
        # dispatches_cross_zone=True. Only applies when source_agent is
        # set (user-initiated dispatches via the dashboard skip this —
        # the user is making an explicit cross-zone decision). Auto-
        # approval rules from Phase 11.2 still apply on top.
        target = session.get(Agent, (workspace_id, agent_name))
        if (
            target is not None
            and source.sensitivity_level != target.sensitivity_level
            and not source.dispatches_cross_zone
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "cross_zone_blocked",
                    "source_agent": body.source_agent,
                    "source_zone": source.sensitivity_level,
                    "target_agent": agent_name,
                    "target_zone": target.sensitivity_level,
                    "message": (
                        f"{body.source_agent!r} ({source.sensitivity_level!r}) "
                        f"cannot dispatch to {agent_name!r} "
                        f"({target.sensitivity_level!r}) — set "
                        f"dispatches_cross_zone=True on the source agent "
                        f"to permit cross-zone dispatches "
                        f"(auto-approval rules still apply on top)."
                    ),
                },
            )
    else:
        depth = 0  # user / off-platform enqueue is a chain root

    # Generate a fresh chain id when none was provided.
    if not chain_id:
        chain_id = str(uuid.uuid4())

    approval_state = _resolve_auto_approval(
        session,
        workspace_id=workspace_id,
        source_agent=body.source_agent,
        target_agent=agent_name,
        command_kind=body.kind,
    )

    cmd = Command(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind=body.kind,
        payload=body.payload,
        status="pending",
        source_agent=body.source_agent,
        dispatch_chain_id=chain_id,
        dispatch_depth=depth,
        approval_state=approval_state,
        approved_at=now if approval_state == "auto_approved" else None,
        created_at=now,
        expires_at=now + COMMAND_TTL,
    )
    session.add(cmd)
    session.flush()
    return _serialize_command(cmd)


@app.get("/agents/{agent_name}/commands")
def list_commands(
    agent_name: str,
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    rows = session.execute(
        select(Command)
        .where(
            Command.workspace_id == workspace_id,
            Command.agent_name == agent_name,
        )
        .order_by(desc(Command.created_at))
        .limit(limit)
    ).scalars().all()
    return {"commands": [_serialize_command(c) for c in rows]}


@app.post("/agents/{agent_name}/commands/claim")
def claim_command(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Atomically claim the oldest pending command for this agent.

    Phase 11.2: only commands with approval_state in
    {'approved', 'auto_approved'} are claimable. 'pending' commands
    sit safely until a human acts on them; 'rejected' / 'expired'
    are terminal.

    Uses Postgres `SELECT ... FOR UPDATE SKIP LOCKED` so two agents
    polling concurrently never claim the same command.
    """
    now = utcnow()
    row = session.execute(
        text(
            """
            SELECT id FROM commands
            WHERE workspace_id = :wsid
              AND agent_name = :agent
              AND status = 'pending'
              AND approval_state IN ('approved', 'auto_approved')
              AND expires_at > :now
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"wsid": workspace_id, "agent": agent_name, "now": now},
    ).first()
    if row is None:
        return {"command": None}
    cmd = session.get(Command, row.id)
    if cmd is None:
        return {"command": None}
    cmd.status = "claimed"
    cmd.claimed_at = now
    session.flush()
    return {"command": _serialize_command(cmd)}


# ---------- Phase 21.2: widget chat ingestion + poll + config ---------- #


# Cap on a single user message. The orchestrator (21.6) re-trims for
# the LLM context window; this is the wire-level guard against a
# pasted-PDF-into-the-textarea kind of abuse.
WIDGET_MESSAGE_MAX_LEN = 8000


class WidgetMessageIn(BaseModel):
    """Body for `POST /widget/{public_id}/messages`.

    `conversation_id` is null on a fresh widget open + populated on
    every subsequent message in the same thread. `anon_user_id` is
    an opaque string the iframe stamps on localStorage (so a
    returning visitor on the same site lands in their previous
    conversation by passing the prior conversation_id, AND the
    workspace's inbox can group anonymous conversations from the
    same end user)."""
    conversation_id: Optional[str] = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=WIDGET_MESSAGE_MAX_LEN)
    anon_user_id: Optional[str] = Field(default=None, max_length=64)


@app.post("/widget/{public_id}/messages", status_code=202)
def widget_post_message(
    public_id: str,
    body: WidgetMessageIn,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.2 + Phase 25.4: widget chat ingress.

    Anonymous by default — the iframe POSTs without an Authorization
    header. Phase 25.4 adds optional end-user auth: when the iframe
    sends an `Authorization: Bearer <end_user_session_token>` from a
    signed-in end user who is linked to this workspace, the
    conversation is scoped to `widget_conversations.end_user_id` so
    the end user's threads follow them across devices.

    Endpoint steps:

      1. Resolves the workspace by `public_id` (404 on miss).
      2. Enforces the Origin allowlist (403 on mismatch).
      3. Optional end-user auth: parse + validate the bearer if
         present. Invalid bearer = 401 immediately (so a stale token
         doesn't silently dump the user's identified state into an
         anonymous conversation).
      4. Per-conversation + per-workspace rate limit (429 on overrun).
      5. Persists the user message + (for new conversations) starts
         a `widget_conversations` row, stamping `end_user_id` only
         when the end user is actively linked to this workspace.
      6. Enqueues a `widget_chat` job for 21.6's orchestrator to
         pick up. The bot response lands as a separate message that
         the widget picks up via the poll endpoint below.
      7. Returns 202 Accepted with the conversation_id + message_id.
    """
    import widget_endpoints as _we
    import limits
    import jobs as _jobs
    from end_user_auth import resolve_end_user_optional

    workspace = _we.resolve_workspace_by_public_id(session, public_id)
    # Phase 31.x: resolve end-user BEFORE the Origin check so an
    # authenticated bearer (native iOS app, web /c page) bypasses
    # the iframe-anti-CSRF allowlist. The same eu_auth is reused
    # for the linkage gate below.
    eu_auth = resolve_end_user_optional(authorization, session)
    _we.check_widget_origin(
        workspace,
        request.headers.get("origin"),
        is_authenticated_end_user=eu_auth is not None,
    )

    if not workspace.customer_facing_agent_name:
        # No bot wired up yet. Surface a 503 rather than enqueueing a
        # doomed job. The operator's first job after pasting the
        # snippet is picking a bot on the settings page.
        raise HTTPException(
            status_code=503,
            detail={
                "error": "widget_unconfigured",
                "message": (
                    "no customer-facing bot is set for this workspace yet. "
                    "Ask the operator to configure one on the widget "
                    "settings page."
                ),
            },
        )

    # Phase 25.4 + Phase 27.6: optional end-user auth. Strict on
    # present-but-invalid so a stale token surfaces as 401 instead
    # of silently degrading. POST is the WRITE path so we use the
    # active-link gate; soft-revoked links can't send new messages
    # per Phase 27 spec. (The GET endpoint below uses
    # can_read_workspace, which includes soft-revoked links so an
    # unsubscribed end user keeps read access to past conversations.)
    # `eu_auth` was already resolved above for the Origin bypass.
    linked = (
        eu_auth is not None
        and eu_auth.can_write_workspace(workspace.id)
    )
    current_end_user_id = eu_auth.end_user.id if linked else None

    # Apply rate limits BEFORE writing anything (Phase 11B-style
    # ordering: reject early, persist late).
    for key, lim, window in _we.widget_message_rate_limit_keys(
        workspace.id, body.conversation_id,
    ):
        limits.rate_limit(key, limit=lim, window_s=window)

    now = utcnow()
    conv: Optional[WidgetConversation] = None
    if body.conversation_id:
        # Existing conversation. Verify it belongs to this workspace
        # — 404 if not (so a leaked id from one workspace can't be
        # used to post into another).
        conv = session.get(WidgetConversation, body.conversation_id)
        if conv is None or conv.workspace_id != workspace.id:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "conversation_not_found",
                    "message": (
                        "no conversation with this id on this widget. "
                        "Drop the conversation_id from your next POST "
                        "to start a fresh thread."
                    ),
                },
            )
        # Phase 25.4: identity gate. Identified callers can only reach
        # their own threads (404 on others'); anonymous callers can
        # only reach unidentified threads (404 on identified ones).
        # Both rules: conv.end_user_id must equal current_end_user_id.
        if conv.end_user_id != current_end_user_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "conversation_not_found",
                    "message": (
                        "no conversation with this id on this widget."
                    ),
                },
            )
        # If the operator has taken over (status='operator_owned'),
        # the bot is paused; the user's message is still recorded
        # but no orchestrator job is enqueued. 21.8 will surface this
        # as a typing indicator on the operator side.

    if conv is None:
        conv = WidgetConversation(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            customer_facing_agent_name=workspace.customer_facing_agent_name,
            status="open",
            # Phase 25.4: identified callers leave anon_user_id NULL
            # (the audit trail uses end_user_id). Anonymous keeps the
            # existing per-device cookie value.
            anon_user_id=body.anon_user_id if current_end_user_id is None else None,
            end_user_id=current_end_user_id,
            started_at=now,
            last_message_at=now,
        )
        session.add(conv)
        session.flush()

    # Persist the user message.
    msg = WidgetMessage(
        conversation_id=conv.id,
        role="user",
        text=body.text,
        sent_at=now,
    )
    session.add(msg)
    conv.last_message_at = now
    session.flush()

    # Enqueue the orchestrator job unless the operator has taken
    # over — in that mode the bot is paused.
    job_id: Optional[str] = None
    if conv.status != "operator_owned":
        job_id = str(uuid.uuid4())
        _jobs.enqueue_job(
            session,
            job_id=job_id,
            workspace_id=workspace.id,
            kind="widget_chat",
            request_payload={
                "conversation_id": conv.id,
                "user_message_id": msg.id,
            },
        )

    return {
        "conversation_id": conv.id,
        "message_id": msg.id,
        "job_id": job_id,
    }


@app.get("/widget/{public_id}/conversations/{conversation_id}")
def widget_get_conversation(
    public_id: str,
    conversation_id: str,
    request: Request,
    since: Optional[int] = None,
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.2 + Phase 25.4: widget polls this for new messages.

    Returns the conversation's messages with id > `since` (cursor
    semantics: the widget tracks the highest message id it's seen
    and asks for everything after). On first poll the widget passes
    `since=0` (or omits it) to get the full history.

    Same Origin enforcement as the POST endpoint. Phase 25.4: same
    optional end-user auth as POST — identified callers can only
    poll their own threads, anonymous callers can only poll
    unidentified threads.
    """
    import widget_endpoints as _we
    from end_user_auth import resolve_end_user_optional

    workspace = _we.resolve_workspace_by_public_id(session, public_id)
    # Phase 31.x: resolve end-user BEFORE the Origin check so an
    # authenticated bearer bypasses the iframe-anti-CSRF allowlist.
    eu_auth = resolve_end_user_optional(authorization, session)
    _we.check_widget_origin(
        workspace,
        request.headers.get("origin"),
        is_authenticated_end_user=eu_auth is not None,
    )

    # Phase 25.4 + Phase 27.6: identity gate. The GET path uses the
    # read-gate (active OR soft-revoked link) so an unsubscribed end
    # user can still read past conversations per Phase 27 spec. The
    # POST path uses the write-gate (active link only) so they can't
    # send new messages.
    can_read = (
        eu_auth is not None
        and eu_auth.can_read_workspace(workspace.id)
    )
    current_end_user_id = eu_auth.end_user.id if can_read else None

    conv = session.get(WidgetConversation, conversation_id)
    if (
        conv is None
        or conv.workspace_id != workspace.id
        or conv.end_user_id != current_end_user_id
    ):
        raise HTTPException(
            status_code=404,
            detail={"error": "conversation_not_found"},
        )

    cursor = since or 0
    rows = session.execute(
        select(WidgetMessage)
        .where(
            WidgetMessage.conversation_id == conv.id,
            WidgetMessage.id > cursor,
        )
        .order_by(WidgetMessage.id)
    ).scalars().all()

    return {
        "conversation_id": conv.id,
        "status": conv.status,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "text": m.text,
                "sent_at": m.sent_at.isoformat(),
            }
            for m in rows
        ],
    }


@app.get("/widget/{public_id}/config")
def widget_get_config(
    public_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.2: iframe fetches the bot's display config on load.

    Returns the customer-facing bot's display name (so the iframe
    header shows "Talk to Vega" instead of a generic "Lightsei
    Assistant"). Plus a small handful of safe-to-expose fields so
    the iframe (21.3) can render trust-zone disclosure + a
    "Powered by Lightsei" footer.

    Notably NOT returned: workspace id, workspace name, operator
    email, any agent system prompt, capabilities, or sensitivity
    level. The end user has no Lightsei account; the surface area
    they see is the bot, not the workspace.

    Same Origin enforcement as POST — a curl from anywhere else
    on the internet can't enumerate which workspaces have widget
    configs.
    """
    import widget_endpoints as _we

    workspace = _we.resolve_workspace_by_public_id(session, public_id)
    _we.check_widget_origin(workspace, request.headers.get("origin"))

    bot_name = workspace.customer_facing_agent_name
    bot_display: Optional[dict[str, Any]] = None
    if bot_name:
        # The agent row might not exist (operator could have deleted
        # the bot after setting it as customer-facing). Surface a
        # placeholder so the iframe still renders rather than erroring.
        agent = session.get(Agent, (workspace.id, bot_name))
        if agent is not None:
            bot_display = {
                "name": agent.name,
                "description": agent.description,
                "sensitivity_level": agent.sensitivity_level,
            }

    return {
        "public_id": public_id,
        "bot": bot_display,
        # Anonymous-only in v1; 21B will add a `requires_signed_token`
        # field once signed-token identity ships.
        "anonymous": True,
    }


# ---------- Phase 21.5: bot-side widget response + escalate ---------- #


class WidgetRespondIn(BaseModel):
    """Body for `POST /widget-bot/respond` (Phase 21.5).

    Called from bot code via `lightsei.respond(conversation_id, text)`.
    `source_agent` identifies the bot making the call so the
    capability gate can check its allow-list."""
    source_agent: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=WIDGET_MESSAGE_MAX_LEN)


def _check_widget_capability(
    session: Session,
    workspace_id: str,
    source_agent: str,
    required_capability: str,
) -> Agent:
    """Shared gate for the two widget bot endpoints. Returns the
    Agent row on success; raises 404 / 403 with the same shape as
    /slack/respond so SDK code can map both via LightseiCapabilityError."""
    agent = session.get(Agent, (workspace_id, source_agent))
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent {source_agent!r} not found in this workspace",
        )
    if required_capability not in (agent.capabilities or []):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "capability_missing",
                "capability": required_capability,
                "agent_name": source_agent,
                "granted": list(agent.capabilities or []),
                "message": (
                    f"agent {source_agent!r} does not have the "
                    f"{required_capability!r} capability. Add it via "
                    "PATCH /agents/{name}/capabilities or designate "
                    "this bot as the workspace's customer-facing bot."
                ),
            },
        )
    return agent


def _load_widget_conversation(
    session: Session, workspace_id: str, conversation_id: str,
) -> WidgetConversation:
    """Load a widget conversation scoped to the calling workspace.
    404 on miss OR cross-workspace lookup (defense against leaked
    conversation IDs)."""
    conv = session.get(WidgetConversation, conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "conversation_not_found",
                "message": (
                    f"conversation {conversation_id!r} not in this workspace"
                ),
            },
        )
    return conv


# How many chars of the message body to put in the push notification
# preview. Long enough to give the user useful context; short enough
# that iOS doesn't truncate awkwardly inside a word.
_PUSH_BODY_PREVIEW_CHARS = 140


def _push_notify_end_user_if_subscribed(
    session: Session,
    conv: WidgetConversation,
    *,
    body_text: str,
) -> None:
    """Phase 28.3: best-effort push notification when a bot or
    operator message lands in a widget conversation.

    No-op when:
      - The conversation is anonymous (conv.end_user_id is None).
      - The end-user has no active link OR the link is soft-removed.
      - The link's notification_pref is 'off'.
      - The end-user has no active push subscriptions (push.send
        handles this internally — we still call so the count
        bumps via capture mode in tests).

    Wrapped in try/except so a push failure doesn't take down the
    persist path. The Phase 28.2 send module is best-effort by
    spec; this wrapper just enforces "don't break the main flow"
    on top.
    """
    if conv.end_user_id is None:
        return
    try:
        import push as _push

        link = session.get(
            EndUserVendorLink, (conv.end_user_id, conv.workspace_id),
        )
        if link is None or link.removed_at is not None:
            return
        if link.notification_pref == "off":
            return

        ws = session.get(Workspace, conv.workspace_id)
        vendor_name = ws.name if ws else "Lightsei"
        vendor_slug = ws.vendor_slug if ws else None

        preview = body_text.strip()
        if len(preview) > _PUSH_BODY_PREVIEW_CHARS:
            preview = preview[: _PUSH_BODY_PREVIEW_CHARS - 1] + "…"

        # Deep link into the conversation on the consumer surface.
        # When the vendor hasn't claimed a slug yet, deep-link to /c
        # (the my-bots index) instead.
        deep_link = (
            f"/c/{vendor_slug}/conversation/{conv.id}"
            if vendor_slug
            else "/c"
        )

        _push.send_to_end_user(
            session,
            conv.end_user_id,
            title=vendor_name,
            body=preview,
            deep_link_url=deep_link,
        )

        # Phase 29.4 stub: APNS fan-out for end users with the
        # native iOS app installed. Capture-mode today; goes live
        # when the Apple Developer account lands + the LIGHTSEI_APNS_*
        # env vars are configured. Wrapped in its own try so an
        # APNS failure doesn't tank the web-push that just succeeded.
        try:
            import apns as _apns
            _apns.send_to_end_user(
                session,
                conv.end_user_id,
                title=vendor_name,
                body=preview,
                deep_link_url=deep_link,
            )
        except Exception:
            import logging as _logging
            _logging.getLogger("lightsei.apns").exception(
                "apns notify failed for conv %s (best-effort, "
                "ignoring)", conv.id,
            )
    except Exception:
        # Best-effort: push failures (capture mode glitches, missing
        # subscription rows, transient pywebpush errors) must not
        # break the bot/operator persist path. The push module logs
        # its own errors; we swallow at this layer to keep the HTTP
        # response 200.
        import logging as _logging
        _logging.getLogger("lightsei.push").exception(
            "push notify failed for conv %s (best-effort, "
            "ignoring)", conv.id,
        )


@app.post("/widget-bot/respond")
def widget_bot_respond(
    body: WidgetRespondIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 21.5: bot posts a reply into a widget conversation.

    Same shape as /slack/respond from Phase 19.5. The bot's API key
    + agent name authenticates; the conversation id scopes the
    target. Capability-gated on `widget:respond`. Persists a
    `bot`-role message, bumps `last_message_at`, and surfaces a
    no-op if the conversation has been marked `resolved` (don't
    let a slow bot reply land after a human operator closed the
    thread).
    """
    agent = _check_widget_capability(
        session, workspace_id, body.source_agent, "widget:respond",
    )
    conv = _load_widget_conversation(session, workspace_id, body.conversation_id)

    if conv.status == "resolved":
        # Operator closed the conversation while the bot was thinking.
        # Refuse the post so the end user doesn't see a stale reply
        # land after they were told the conversation was wrapped up.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conversation_resolved",
                "message": (
                    "the conversation was marked resolved before this "
                    "reply landed; the bot reply is being dropped."
                ),
            },
        )

    now = utcnow()
    msg = WidgetMessage(
        conversation_id=conv.id,
        role="bot",
        text=body.text,
        sent_at=now,
    )
    session.add(msg)
    conv.last_message_at = now
    session.flush()
    # Phase 28.3: push notify the identified end user (best-effort
    # no-op on anonymous / opted-out / no-subscriptions).
    _push_notify_end_user_if_subscribed(session, conv, body_text=body.text)
    return {
        "ok": True,
        "message_id": msg.id,
        "conversation_id": conv.id,
    }


class WidgetEscalateIn(BaseModel):
    """Body for `POST /widget-bot/escalate` (Phase 21.5).

    Called from bot code via `lightsei.escalate(conversation_id,
    reason)` OR by raising `LightseiEscalate(reason)` from an
    `@on_chat("widget")` handler. The 21.6 orchestrator catches
    the exception and POSTs to this endpoint on the bot's behalf."""
    source_agent: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


@app.post("/widget-bot/escalate")
def widget_bot_escalate(
    body: WidgetEscalateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 21.5: bot flips a widget conversation into the
    escalated state.

    Sets `conversation.status = 'escalated'`, creates a
    `widget_escalations` row, persists a system message into the
    thread ("This conversation has been handed off to a human."),
    and returns the new escalation id. Idempotent on the
    conversation: if the conversation is already escalated /
    operator_owned / resolved, the call short-circuits without
    creating a duplicate escalation row.

    Capability-gated on `widget:escalate`. The orchestrator's
    LightseiEscalate-exception path also routes through here so
    the gate enforces consistently across the two SDK entry points.
    """
    agent = _check_widget_capability(
        session, workspace_id, body.source_agent, "widget:escalate",
    )
    conv = _load_widget_conversation(session, workspace_id, body.conversation_id)

    if conv.status in ("escalated", "operator_owned", "resolved"):
        # Already in a state that doesn't need a new escalation row.
        # Don't 4xx — the bot doesn't need to handle this case
        # specially; surface a "noop" status so the SDK can log it.
        return {
            "ok": True,
            "status": conv.status,
            "noop": True,
        }

    now = utcnow()
    esc_id = str(uuid.uuid4())
    session.add(WidgetEscalation(
        id=esc_id,
        conversation_id=conv.id,
        reason=body.reason,
        payload=body.payload or {},
        escalated_at=now,
    ))
    conv.status = "escalated"
    conv.last_message_at = now

    # Drop a system message into the thread so the end user sees the
    # handoff land in their iframe (instead of just silence).
    session.add(WidgetMessage(
        conversation_id=conv.id,
        role="system",
        text="This conversation has been handed off to a human.",
        sent_at=now,
    ))
    session.flush()

    return {
        "ok": True,
        "status": "escalated",
        "escalation_id": esc_id,
    }


# ---------- Phase 21.7: operator-facing widget settings ---------- #


# Capabilities the customer-facing bot needs to participate in
# widget chat. Auto-granted on PATCH when an operator designates
# the bot — having the bot picked but unable to reply would be
# a confusing failure mode.
_WIDGET_AUTO_GRANT_CAPABILITIES: tuple[str, ...] = (
    "widget:respond",
    "widget:escalate",
)


def _validate_widget_origin(origin: str) -> Optional[str]:
    """Return an error message if `origin` isn't a valid widget
    allowlist entry; None if it's fine. Operator-facing copy."""
    if not isinstance(origin, str):
        return "must be a string"
    origin = origin.strip()
    if not origin:
        return "must not be empty"
    if len(origin) > 256:
        return "must be 256 characters or fewer"
    if not (origin.startswith("https://") or origin.startswith("http://localhost")):
        return (
            "must start with 'https://' (or 'http://localhost' for "
            "local development)"
        )
    # Reject paths / query / fragment — the snippet runs on the
    # customer's site and the browser only sends origin (scheme +
    # host[:port]) in the Origin header.
    rest = origin.split("://", 1)[1]
    if "/" in rest or "?" in rest or "#" in rest:
        return "must not include a path, query string, or fragment"
    return None


@app.get("/workspaces/me/widget-settings")
def get_widget_settings(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.7: return the widget configuration for this
    workspace + the list of agents that can be designated as the
    customer-facing bot.

    First call mints + persists a `widget_public_id` (via the
    21.2 `ensure_widget_public_id` helper) so subsequent reads
    return the same id. Operator-only — bot API keys can't see
    this surface (they don't need to)."""
    import widget_endpoints as _we

    workspace = session.get(Workspace, auth.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    public_id = _we.ensure_widget_public_id(session, workspace)

    # Available agents: every non-system bot in the workspace, sorted
    # by name. Hide `lightsei.system` + similar internal agents.
    agents = session.execute(
        select(Agent).where(Agent.workspace_id == auth.workspace_id)
        .order_by(Agent.name)
    ).scalars().all()
    available = [
        {
            "name": a.name,
            "description": a.description,
            "sensitivity_level": a.sensitivity_level,
            "has_widget_capabilities": all(
                c in (a.capabilities or [])
                for c in _WIDGET_AUTO_GRANT_CAPABILITIES
            ),
        }
        for a in agents
        if not a.name.startswith("lightsei.")
    ]

    return {
        "widget_public_id": public_id,
        "customer_facing_agent_name": workspace.customer_facing_agent_name,
        "allowed_widget_origins": list(workspace.allowed_widget_origins or []),
        "available_agents": available,
    }


class WidgetSettingsPatchIn(BaseModel):
    """Body for `PATCH /workspaces/me/widget-settings` (Phase 21.7).

    Both fields optional — operators usually edit one at a time
    (pick a bot, then later edit the origins as they add the
    snippet to new sites). At least one must be present."""
    customer_facing_agent_name: Optional[str] = Field(default=None, max_length=128)
    allowed_widget_origins: Optional[list[str]] = Field(default=None)


@app.patch("/workspaces/me/widget-settings")
def patch_widget_settings(
    body: WidgetSettingsPatchIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.7: update the workspace's widget configuration.

    Two surfaces:

    - `customer_facing_agent_name`: which bot answers widget
      conversations. Validated as an existing agent in this
      workspace (404 if not). When set, the bot is auto-granted
      `widget:respond` + `widget:escalate` capabilities — having
      the bot designated but unable to reply would be a confusing
      failure mode. Pass `null` to clear (un-designate the bot;
      widget POSTs will surface 503 widget_unconfigured).
    - `allowed_widget_origins`: HTTPS origins the public widget
      endpoint will accept. Per-entry validation: must be
      https://host[:port] (or http://localhost for dev); no
      paths, queries, or fragments. 422 with a per-entry error
      list if any entry is invalid.
    """
    if (
        body.customer_facing_agent_name is None
        and body.allowed_widget_origins is None
        and "customer_facing_agent_name" not in body.model_fields_set
        and "allowed_widget_origins" not in body.model_fields_set
    ):
        raise HTTPException(
            status_code=422,
            detail="must supply customer_facing_agent_name or allowed_widget_origins",
        )

    workspace = session.get(Workspace, auth.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    # ---- customer_facing_agent_name ---- #
    if "customer_facing_agent_name" in body.model_fields_set:
        new_name = body.customer_facing_agent_name
        if new_name is None or new_name == "":
            # Clear the pointer.
            workspace.customer_facing_agent_name = None
        else:
            agent = session.get(Agent, (auth.workspace_id, new_name))
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "agent_not_found",
                        "agent_name": new_name,
                        "message": (
                            f"agent {new_name!r} doesn't exist in this "
                            "workspace. Deploy the bot first, then pick "
                            "it here."
                        ),
                    },
                )
            workspace.customer_facing_agent_name = new_name
            # Auto-grant the widget capabilities if missing. Operators
            # typically pick a bot without thinking about capabilities,
            # so silently adding them avoids the "I picked a bot but
            # it can't answer" failure mode. Operators can still
            # remove them later via PATCH /agents/{name}/capabilities
            # if they explicitly don't want this bot to use the widget.
            existing = list(agent.capabilities or [])
            changed = False
            for cap in _WIDGET_AUTO_GRANT_CAPABILITIES:
                if cap not in existing:
                    existing.append(cap)
                    changed = True
            if changed:
                agent.capabilities = existing

    # ---- allowed_widget_origins ---- #
    if "allowed_widget_origins" in body.model_fields_set:
        origins = body.allowed_widget_origins or []
        # Dedup + validate. Surface ALL per-entry errors at once so
        # the operator doesn't fix-and-retry one at a time.
        seen: set[str] = set()
        cleaned: list[str] = []
        errors: list[dict[str, Any]] = []
        for i, raw in enumerate(origins):
            err = _validate_widget_origin(raw)
            if err:
                errors.append({"index": i, "value": raw, "error": err})
                continue
            stripped = raw.strip()
            if stripped in seen:
                continue
            seen.add(stripped)
            cleaned.append(stripped)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_widget_origins",
                    "errors": errors,
                    "message": (
                        "one or more allowed_widget_origins entries are "
                        "invalid — see `errors` for details."
                    ),
                },
            )
        workspace.allowed_widget_origins = cleaned

    session.flush()

    # Return the same shape as GET so the dashboard can refresh
    # without a second roundtrip.
    return get_widget_settings(auth=auth, session=session)


# ---------- Phase 21.8: operator inbox endpoints ---------- #


# Conversation list defaults — small enough to render quickly,
# big enough that the dashboard rarely needs to paginate. Cursor-
# based pagination is parked to a future surface; the inbox is
# typically small (tens of conversations at v1 scale).
INBOX_DEFAULT_LIMIT = 50
INBOX_MAX_LIMIT = 200
INBOX_PREVIEW_CHARS = 140


_INBOX_STATUS_FILTERS = {
    "all",
    "open",
    "escalated",
    "operator_owned",
    "resolved",
    # Convenience grouping: anything still needing attention. Useful
    # default for the "open + escalated + operator_owned" view that
    # excludes resolved threads.
    "active",
}


def _serialize_inbox_conversation_row(
    conv: "WidgetConversation",
    *,
    agent: Optional["Agent"],
    last_message: Optional["WidgetMessage"],
    open_escalation_count: int,
) -> dict[str, Any]:
    """Per-row shape for the conversation list. Compact —
    drops full message bodies + escalation payloads."""
    preview = ""
    if last_message and last_message.text:
        preview = last_message.text[:INBOX_PREVIEW_CHARS]
        if len(last_message.text) > INBOX_PREVIEW_CHARS:
            preview = preview + "…"
    return {
        "id": conv.id,
        "status": conv.status,
        "customer_facing_agent_name": conv.customer_facing_agent_name,
        # Snapshot the bot's current zone for the row; if the agent
        # was deleted, surface null so the dashboard chip falls back
        # to a placeholder rather than crashing.
        "sensitivity_level": agent.sensitivity_level if agent else None,
        "anon_user_id": conv.anon_user_id,
        "started_at": conv.started_at.isoformat(),
        "last_message_at": conv.last_message_at.isoformat(),
        "resolved_at": conv.resolved_at.isoformat() if conv.resolved_at else None,
        "open_escalation_count": open_escalation_count,
        "last_message_preview": preview,
        "last_message_role": last_message.role if last_message else None,
    }


@app.get("/workspaces/me/inbox")
def list_inbox(
    status: str = "active",
    since: Optional[str] = None,
    limit: int = INBOX_DEFAULT_LIMIT,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.8: list widget conversations for the operator inbox.

    Filters (`?status=`):
      - all              → every conversation
      - active (default) → open + escalated + operator_owned
      - open / escalated / operator_owned / resolved → exact match

    `?since=` is an ISO timestamp; only returns conversations with
    `last_message_at > since`. Drives the dashboard's polling refresh
    without re-rendering everything.

    Ordering: escalated rows bumped to the top (they need attention),
    then everyone else by `last_message_at DESC` (most-recently-active
    first).
    """
    if status not in _INBOX_STATUS_FILTERS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_status_filter",
                "valid": sorted(_INBOX_STATUS_FILTERS),
            },
        )
    limit = max(1, min(INBOX_MAX_LIMIT, int(limit)))

    q = select(WidgetConversation).where(
        WidgetConversation.workspace_id == auth.workspace_id
    )
    if status == "active":
        q = q.where(WidgetConversation.status.in_(
            ["open", "escalated", "operator_owned"]
        ))
    elif status != "all":
        q = q.where(WidgetConversation.status == status)

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="since must be an ISO timestamp",
            )
        q = q.where(WidgetConversation.last_message_at > since_dt)

    # Pull the conversations first. Order is applied after we layer
    # in the "escalated first" sort in Python so we don't need a
    # complicated SQL CASE expression.
    q = q.order_by(WidgetConversation.last_message_at.desc()).limit(limit)
    conversations = session.execute(q).scalars().all()

    # Bulk-fetch the customer-facing agents + last message per
    # conversation + open escalation counts to avoid N+1.
    agent_keys = {
        (auth.workspace_id, c.customer_facing_agent_name)
        for c in conversations
        if c.customer_facing_agent_name
    }
    agents_by_key: dict[tuple[str, str], "Agent"] = {}
    if agent_keys:
        agent_rows = session.execute(
            select(Agent).where(
                Agent.workspace_id == auth.workspace_id,
                Agent.name.in_({k[1] for k in agent_keys}),
            )
        ).scalars().all()
        agents_by_key = {(a.workspace_id, a.name): a for a in agent_rows}

    # Last message per conversation. Cheap one-shot query against
    # the (conversation_id, sent_at) index from Phase 21.1.
    last_messages_by_conv: dict[str, WidgetMessage] = {}
    if conversations:
        conv_ids = [c.id for c in conversations]
        # Per-conversation latest: subquery for max(id), then join.
        # The Phase 21.1 index on (conversation_id, sent_at) makes
        # this cheap.
        from sqlalchemy import func as _func
        latest_ids = session.execute(
            select(
                WidgetMessage.conversation_id,
                _func.max(WidgetMessage.id).label("max_id"),
            )
            .where(WidgetMessage.conversation_id.in_(conv_ids))
            .group_by(WidgetMessage.conversation_id)
        ).all()
        max_id_set = [row.max_id for row in latest_ids]
        if max_id_set:
            last_msg_rows = session.execute(
                select(WidgetMessage).where(WidgetMessage.id.in_(max_id_set))
            ).scalars().all()
            last_messages_by_conv = {
                m.conversation_id: m for m in last_msg_rows
            }

    # Open escalations counted per conversation. Used by the row
    # renderer to badge "needs attention" conversations.
    open_escalation_counts: dict[str, int] = {}
    if conversations:
        from sqlalchemy import func as _func
        counts = session.execute(
            select(
                WidgetEscalation.conversation_id,
                _func.count(WidgetEscalation.id).label("n"),
            )
            .where(
                WidgetEscalation.conversation_id.in_(
                    [c.id for c in conversations]
                ),
                WidgetEscalation.resolved_at.is_(None),
            )
            .group_by(WidgetEscalation.conversation_id)
        ).all()
        open_escalation_counts = {row.conversation_id: row.n for row in counts}

    rows = [
        _serialize_inbox_conversation_row(
            c,
            agent=agents_by_key.get((auth.workspace_id, c.customer_facing_agent_name or "")),
            last_message=last_messages_by_conv.get(c.id),
            open_escalation_count=open_escalation_counts.get(c.id, 0),
        )
        for c in conversations
    ]

    # Bump escalated rows to the top. Stable sort preserves the
    # last_message_at DESC ordering within each group.
    def _sort_key(row):
        # Lower priority value = sorted earlier.
        if row["status"] == "escalated":
            return 0
        if row["status"] == "operator_owned":
            return 1
        return 2

    rows.sort(key=_sort_key)

    return {
        "conversations": rows,
        "filter": status,
        "limit": limit,
        # Server's current time helps the dashboard compute a fresh
        # since cursor for the next poll without trusting client clocks.
        "as_of": utcnow().isoformat(),
    }


def _load_inbox_conversation(
    session: Session, workspace_id: str, conversation_id: str,
) -> "WidgetConversation":
    """Shared helper: load a conversation scoped to the workspace.
    Same shape as the helper the bot endpoints use."""
    conv = session.get(WidgetConversation, conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "conversation_not_found",
                "message": (
                    f"conversation {conversation_id!r} not in this workspace"
                ),
            },
        )
    return conv


@app.get("/workspaces/me/inbox/{conversation_id}")
def get_inbox_conversation(
    conversation_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.8: thread view for a single conversation.

    Returns the full conversation metadata, the message list in
    chronological order, and any escalation rows (resolved + open).
    Operators read this to triage + decide whether to take over.
    """
    conv = _load_inbox_conversation(session, auth.workspace_id, conversation_id)

    messages = session.execute(
        select(WidgetMessage)
        .where(WidgetMessage.conversation_id == conv.id)
        .order_by(WidgetMessage.sent_at, WidgetMessage.id)
    ).scalars().all()

    escalations = session.execute(
        select(WidgetEscalation)
        .where(WidgetEscalation.conversation_id == conv.id)
        .order_by(WidgetEscalation.escalated_at)
    ).scalars().all()

    agent = None
    if conv.customer_facing_agent_name:
        agent = session.get(
            Agent, (auth.workspace_id, conv.customer_facing_agent_name)
        )

    return {
        "id": conv.id,
        "status": conv.status,
        "customer_facing_agent_name": conv.customer_facing_agent_name,
        "sensitivity_level": agent.sensitivity_level if agent else None,
        "anon_user_id": conv.anon_user_id,
        "started_at": conv.started_at.isoformat(),
        "last_message_at": conv.last_message_at.isoformat(),
        "resolved_at": conv.resolved_at.isoformat() if conv.resolved_at else None,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "text": m.text,
                "sent_at": m.sent_at.isoformat(),
            }
            for m in messages
        ],
        "escalations": [
            {
                "id": e.id,
                "reason": e.reason,
                "payload": e.payload or {},
                "suggested_fix": e.suggested_fix,
                "escalated_at": e.escalated_at.isoformat(),
                "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
            }
            for e in escalations
        ],
    }


@app.post("/workspaces/me/inbox/{conversation_id}/take-over")
def inbox_take_over(
    conversation_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.8: operator takes over the conversation.

    Flips `status='operator_owned'`, drops a system message into the
    thread (so the end user's iframe shows the handoff), bumps
    `last_message_at`. Idempotent on already-operator_owned (returns
    `noop: true`). Refuses on `resolved` (operator should re-open
    explicitly — not in v1).

    Once the conversation is operator_owned, 21.2's POST /messages
    skips enqueueing the orchestrator job (bot is paused) and 21.6's
    orchestrator skips dispatching. Subsequent end-user messages
    are recorded but no bot reply lands.
    """
    conv = _load_inbox_conversation(session, auth.workspace_id, conversation_id)

    if conv.status == "operator_owned":
        return {"ok": True, "status": "operator_owned", "noop": True}
    if conv.status == "resolved":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conversation_resolved",
                "message": (
                    "this conversation has been resolved; reopening from "
                    "the inbox is not supported in v1."
                ),
            },
        )

    now = utcnow()
    conv.status = "operator_owned"
    conv.last_message_at = now
    session.add(WidgetMessage(
        conversation_id=conv.id,
        role="system",
        text="An operator has joined the conversation.",
        sent_at=now,
    ))
    session.flush()
    return {"ok": True, "status": "operator_owned"}


class InboxOperatorReplyIn(BaseModel):
    """Body for `POST /workspaces/me/inbox/{id}/messages` (Phase 21.8).

    Operator types a reply directly into the conversation. The bot
    is paused (conversation should be `operator_owned`) so the
    reply doesn't race with bot output."""
    text: str = Field(min_length=1, max_length=WIDGET_MESSAGE_MAX_LEN)


@app.post("/workspaces/me/inbox/{conversation_id}/messages")
def inbox_operator_reply(
    conversation_id: str,
    body: InboxOperatorReplyIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.8: persist an operator-typed reply into the
    conversation.

    Refuses if the conversation is `resolved` (don't let an operator
    accidentally re-open a closed thread by typing). Allowed on
    `open`/`escalated`/`operator_owned` — operators can also reply
    to an escalation row without taking over first (the system
    message remains; the operator just chimes in).
    """
    conv = _load_inbox_conversation(session, auth.workspace_id, conversation_id)
    if conv.status == "resolved":
        raise HTTPException(
            status_code=409,
            detail={"error": "conversation_resolved"},
        )

    now = utcnow()
    msg = WidgetMessage(
        conversation_id=conv.id,
        role="operator",
        text=body.text,
        sent_at=now,
    )
    session.add(msg)
    conv.last_message_at = now
    session.flush()
    # Phase 28.3: same push hook as widget-bot/respond. Identified
    # end users with notification_pref != 'off' get notified that
    # the operator just chimed in.
    _push_notify_end_user_if_subscribed(session, conv, body_text=body.text)
    return {
        "ok": True,
        "message_id": msg.id,
        "conversation_id": conv.id,
    }


@app.post("/workspaces/me/inbox/{conversation_id}/resolve")
def inbox_resolve(
    conversation_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.8: operator wraps up the conversation.

    Sets `status='resolved'`, stamps `resolved_at`, drops a system
    message into the thread, and resolves any open escalation rows
    (stamps each one's `resolved_at` + `resolved_by_user_id` to the
    operator). Idempotent on already-resolved.
    """
    conv = _load_inbox_conversation(session, auth.workspace_id, conversation_id)
    if conv.status == "resolved":
        return {"ok": True, "status": "resolved", "noop": True}

    now = utcnow()
    conv.status = "resolved"
    conv.resolved_at = now
    conv.last_message_at = now

    # Resolve any open escalations on the conversation. Stamp the
    # operator as the resolver so the audit trail captures who
    # closed it.
    open_escalations = session.execute(
        select(WidgetEscalation).where(
            WidgetEscalation.conversation_id == conv.id,
            WidgetEscalation.resolved_at.is_(None),
        )
    ).scalars().all()
    operator_user_id = (
        auth.user.id if auth.user else None
    )
    for esc in open_escalations:
        esc.resolved_at = now
        esc.resolved_by_user_id = operator_user_id

    session.add(WidgetMessage(
        conversation_id=conv.id,
        role="system",
        text="This conversation has been marked resolved by an operator.",
        sent_at=now,
    ))
    session.flush()
    return {
        "ok": True,
        "status": "resolved",
        "resolved_escalation_count": len(open_escalations),
    }


# ---------- Phase 21.9: Polaris widget-incident-response ---------- #


@app.post("/workspaces/me/widget-incident-response/scan")
def scan_widget_incident_patterns(
    lookback_hours: int = 24,
    min_size: int = 3,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.9: scan open widget escalations for patterns + write
    a suggested_fix on the rows in each detected cluster.

    Pipeline (each step a no-op if its precondition isn't met —
    scans never fail just because there's nothing to do):

      1. Cluster open escalations from the last `lookback_hours`
         (default 24) grouping by `reason` + token-overlap on the
         escalating user message. Drop clusters smaller than
         `min_size` (default 3).
      2. For each cluster, call Anthropic to draft a `suggested_fix`
         dict. Anthropic failures are swallowed per-cluster (logged
         + skip that cluster).
      3. Persist the suggested_fix on every escalation row in the
         cluster so the inbox can surface it.
      4. Emit a `polaris.issue_pattern` event per cluster so the
         dashboard's Polaris insights surface (12D.2) picks it up
         alongside the existing cost-analysis patterns.
      5. If the workspace has `polaris_auto_apply_widget_fixes` set,
         immediately apply the fix to the customer-facing bot's
         system_prompt + resolve the escalations + drop a system
         message in each conversation.

    Returns a summary: `{clusters_found, fixes_generated,
    fixes_applied, conversations_touched}`.
    """
    import widget_incident_response as _wir

    workspace = session.get(Workspace, auth.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    clusters = _wir.find_escalation_clusters(
        session, auth.workspace_id,
        lookback_hours=max(1, min(168, int(lookback_hours))),
        min_size=max(2, min(50, int(min_size))),
    )

    if not clusters:
        return {
            "clusters_found": 0,
            "fixes_generated": 0,
            "fixes_applied": 0,
            "conversations_touched": 0,
        }

    # Anthropic key for fix generation. If unset, surface a 400 —
    # operators need to know the scan can't run without it.
    secret_row = session.get(
        WorkspaceSecret, (auth.workspace_id, "ANTHROPIC_API_KEY")
    )
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_anthropic_key",
                "message": (
                    "this workspace has no ANTHROPIC_API_KEY secret set. "
                    "Add it on /account before running the scan."
                ),
            },
        )

    import secrets_crypto
    try:
        anthropic_key = secrets_crypto.decrypt(secret_row.encrypted_value)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="couldn't decrypt ANTHROPIC_API_KEY",
        )

    # Commit before the LLM calls — Railway Postgres kills
    # idle-in-transaction connections during multi-second waits.
    # Same pattern as slack_orchestrator / team_planner.
    session.commit()

    fixes_generated = 0
    fixes_applied = 0
    conversations_touched_set: set[str] = set()
    auto_apply = bool(workspace.polaris_auto_apply_widget_fixes)

    for cluster in clusters:
        fix = _wir.generate_suggested_fix(cluster, anthropic_key)
        if fix is None:
            continue
        fixes_generated += 1

        # Persist suggested_fix on every escalation in the cluster.
        # Cluster size capped by the scan's min_size; bulk-update.
        escalation_rows = session.execute(
            select(WidgetEscalation).where(
                WidgetEscalation.id.in_(cluster["escalation_ids"])
            )
        ).scalars().all()
        for esc in escalation_rows:
            esc.suggested_fix = fix

        # Emit a polaris.issue_pattern event so the existing 12D.2
        # insights surface picks it up alongside cost-analysis
        # patterns. Run row is named lightsei.system (same as
        # cost-analysis events) so the dashboard's Polaris view
        # groups them together.
        _emit_issue_pattern_event(
            session,
            workspace_id=auth.workspace_id,
            cluster=cluster,
            fix=fix,
        )

        if auto_apply:
            # Apply the fix immediately. _apply_widget_suggested_fix
            # handles the agent mutation + escalation resolution +
            # system message + counts.
            applied = _apply_widget_suggested_fix(
                session,
                workspace_id=auth.workspace_id,
                escalation_rows=escalation_rows,
                fix=fix,
                operator_user_id=None,  # auto-applied; no operator
            )
            fixes_applied += 1
            conversations_touched_set.update(applied["conversations_touched"])

        session.flush()

    return {
        "clusters_found": len(clusters),
        "fixes_generated": fixes_generated,
        "fixes_applied": fixes_applied,
        "conversations_touched": len(conversations_touched_set),
        "auto_apply_enabled": auto_apply,
    }


def _emit_issue_pattern_event(
    session: Session,
    *,
    workspace_id: str,
    cluster: dict[str, Any],
    fix: dict[str, Any],
) -> None:
    """Drop a polaris.issue_pattern event on a lightsei.system run.
    Same pattern as 12D.2's cost_analysis events so they show up
    side-by-side in /polaris."""
    now = utcnow()
    run_id = str(uuid.uuid4())
    session.add(Run(
        id=run_id,
        workspace_id=workspace_id,
        agent_name="lightsei.system",
        started_at=now,
        ended_at=now,
        sensitivity_level="internal",
    ))
    session.flush()
    session.add(Event(
        workspace_id=workspace_id,
        run_id=run_id,
        agent_name="lightsei.system",
        kind="polaris.issue_pattern",
        payload={
            "reason": cluster["reason"],
            "size": cluster["size"],
            "keywords": cluster.get("keywords") or [],
            "escalation_ids": cluster["escalation_ids"],
            "suggested_fix": fix,
        },
        timestamp=now,
    ))
    session.flush()


def _apply_widget_suggested_fix(
    session: Session,
    *,
    workspace_id: str,
    escalation_rows: list["WidgetEscalation"],
    fix: dict[str, Any],
    operator_user_id: Optional[str],
) -> dict[str, Any]:
    """Apply a suggested fix to the bot and resolve the escalations.

    Mutates the customer-facing bot's `system_prompt` with the
    addendum + stamps the cluster's escalations resolved + drops a
    system message into each affected conversation. Pure side-effect
    helper; returns a small summary the caller stitches into the
    scan / endpoint response.
    """
    import widget_incident_response as _wir

    now = utcnow()
    conversations_touched: set[str] = set()
    agents_mutated: set[str] = set()

    # Pull all the conversations the cluster spans + their bots in
    # one query each. Cluster size is bounded by min_size; cost is
    # small.
    conv_ids = {e.conversation_id for e in escalation_rows}
    convs = session.execute(
        select(WidgetConversation).where(
            WidgetConversation.id.in_(conv_ids),
            WidgetConversation.workspace_id == workspace_id,
        )
    ).scalars().all()
    convs_by_id = {c.id: c for c in convs}

    # Bot mutation: snap the system_prompt on each unique customer-
    # facing bot referenced in the cluster. Usually all rows point
    # at the same bot (one workspace, one customer-facing bot), but
    # the data shape allows divergence (e.g. operator swapped the
    # bot mid-window).
    bot_names = {
        c.customer_facing_agent_name
        for c in convs
        if c.customer_facing_agent_name
    }
    for name in bot_names:
        agent = session.get(Agent, (workspace_id, name))
        if agent is None:
            continue
        agent.system_prompt = _wir.append_fix_to_system_prompt(
            agent.system_prompt, fix, applied_at=now,
        )
        agent.updated_at = now
        agents_mutated.add(name)

    # Resolve each escalation + drop a system message in its thread.
    for esc in escalation_rows:
        if esc.resolved_at is not None:
            # Already resolved; skip (idempotent).
            continue
        esc.resolved_at = now
        esc.resolved_by_user_id = operator_user_id

        conv = convs_by_id.get(esc.conversation_id)
        if conv is None:
            continue
        # Flip back to open so the bot can take new messages with
        # the updated system_prompt. Status is operator_owned /
        # escalated → open. Resolved threads stay resolved.
        if conv.status in ("escalated", "operator_owned"):
            conv.status = "open"
        conv.last_message_at = now
        session.add(WidgetMessage(
            conversation_id=conv.id,
            role="system",
            text=(
                "Polaris updated the bot based on this conversation "
                "and similar ones. The bot will try again with new "
                "guidance."
            ),
            sent_at=now,
        ))
        conversations_touched.add(conv.id)

    session.flush()
    return {
        "conversations_touched": conversations_touched,
        "agents_mutated": agents_mutated,
    }


@app.post(
    "/workspaces/me/inbox/{conversation_id}/escalations/"
    "{escalation_id}/apply-fix"
)
def apply_escalation_suggested_fix(
    conversation_id: str,
    escalation_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.9: operator applies the Polaris-suggested fix on
    an escalation.

    Mutates the bot's system_prompt with the addendum + marks the
    escalation resolved + drops a system message in the conversation
    + flips the conversation back to `open` so the bot retries with
    the new guidance. 404 if the escalation isn't on this
    workspace's conversation; 409 if it has no suggested_fix; 409
    if the fix has already been applied (escalation resolved)."""
    conv = _load_inbox_conversation(session, auth.workspace_id, conversation_id)

    esc = session.get(WidgetEscalation, escalation_id)
    if esc is None or esc.conversation_id != conv.id:
        raise HTTPException(
            status_code=404,
            detail={"error": "escalation_not_found"},
        )

    if not esc.suggested_fix:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_suggested_fix",
                "message": (
                    "this escalation has no Polaris-suggested fix. "
                    "Run the scan first."
                ),
            },
        )

    if esc.resolved_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "escalation_already_resolved",
                "message": "this escalation was already resolved.",
            },
        )

    applied = _apply_widget_suggested_fix(
        session,
        workspace_id=auth.workspace_id,
        escalation_rows=[esc],
        fix=esc.suggested_fix,
        operator_user_id=(auth.user.id if auth.user else None),
    )
    return {
        "ok": True,
        "applied": True,
        "conversations_touched": list(applied["conversations_touched"]),
        "agents_mutated": list(applied["agents_mutated"]),
    }


@app.post(
    "/workspaces/me/inbox/{conversation_id}/escalations/"
    "{escalation_id}/dismiss-fix"
)
def dismiss_escalation_suggested_fix(
    conversation_id: str,
    escalation_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 21.9: operator dismisses the suggested fix. Clears
    `suggested_fix` on the escalation row; leaves the escalation
    open so the operator can still take over / resolve normally.
    Idempotent on already-dismissed."""
    conv = _load_inbox_conversation(session, auth.workspace_id, conversation_id)

    esc = session.get(WidgetEscalation, escalation_id)
    if esc is None or esc.conversation_id != conv.id:
        raise HTTPException(
            status_code=404,
            detail={"error": "escalation_not_found"},
        )

    if not esc.suggested_fix:
        return {"ok": True, "dismissed": True, "noop": True}

    esc.suggested_fix = None
    session.flush()
    return {"ok": True, "dismissed": True}


# ---------- Phase 11.2: approval endpoints + auto-approval CRUD ---------- #


@app.post("/commands/{command_id}/approve")
def approve_command(
    command_id: str,
    body: CommandApprovalIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Flip a pending command to 'approved' so the next claim picks
    it up. Only works on commands in the caller's workspace and only
    when approval_state is currently 'pending' (idempotent flips
    would be confusing — a re-approve isn't meaningfully different
    from the first one)."""
    workspace_id = auth.workspace_id
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.approval_state != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"command already {cmd.approval_state!r}",
        )
    cmd.approval_state = "approved"
    cmd.approved_by_user_id = auth.user.id if auth.user else None
    cmd.approved_at = utcnow()
    session.flush()
    return _serialize_command(cmd)


@app.post("/commands/{command_id}/reject")
def reject_command(
    command_id: str,
    body: CommandApprovalIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Flip a pending command to 'rejected' (terminal). Records the
    user who rejected on approved_by_user_id so the audit trail is
    consistent — the column reflects "who acted on the gate," not
    specifically who said yes."""
    workspace_id = auth.workspace_id
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.approval_state != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"command already {cmd.approval_state!r}",
        )
    cmd.approval_state = "rejected"
    cmd.status = "cancelled"
    cmd.approved_by_user_id = auth.user.id if auth.user else None
    cmd.approved_at = utcnow()
    cmd.completed_at = utcnow()
    if body.reason:
        cmd.error = f"rejected: {body.reason}"
    session.flush()
    return _serialize_command(cmd)


def _serialize_auto_approval_rule(r: CommandAutoApprovalRule) -> dict[str, Any]:
    return {
        "source_agent": r.source_agent,
        "target_agent": r.target_agent,
        "command_kind": r.command_kind,
        "mode": r.mode,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


@app.get("/workspaces/me/auto-approval-rules")
def list_auto_approval_rules(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(CommandAutoApprovalRule)
        .where(CommandAutoApprovalRule.workspace_id == workspace_id)
        .order_by(
            CommandAutoApprovalRule.source_agent,
            CommandAutoApprovalRule.target_agent,
            CommandAutoApprovalRule.command_kind,
        )
    ).scalars().all()
    return {"rules": [_serialize_auto_approval_rule(r) for r in rows]}


@app.put("/workspaces/me/auto-approval-rules")
def upsert_auto_approval_rule(
    body: AutoApprovalRuleIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Upsert by composite PK so the same call creates a new rule or
    updates an existing one. PK is (workspace, source, target, kind),
    which means flipping a rule from 'auto_approve' to 'require_human'
    on the same triple lands as a single update."""
    now = utcnow()
    existing = session.get(
        CommandAutoApprovalRule,
        (workspace_id, body.source_agent, body.target_agent, body.command_kind),
    )
    if existing is None:
        rule = CommandAutoApprovalRule(
            workspace_id=workspace_id,
            source_agent=body.source_agent,
            target_agent=body.target_agent,
            command_kind=body.command_kind,
            mode=body.mode,
            created_at=now,
            updated_at=now,
        )
        session.add(rule)
    else:
        existing.mode = body.mode
        existing.updated_at = now
        rule = existing
    session.flush()
    return _serialize_auto_approval_rule(rule)


@app.delete("/workspaces/me/auto-approval-rules")
def delete_auto_approval_rule(
    source_agent: str,
    target_agent: str,
    command_kind: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    rule = session.get(
        CommandAutoApprovalRule,
        (workspace_id, source_agent, target_agent, command_kind),
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    session.delete(rule)
    session.flush()
    return {"status": "ok"}


# ---------- Phase 11.6: dispatch-chain views ---------- #


def _aggregate_chain_status(commands: list[Command]) -> str:
    """Roll up a chain's overall status from its commands.

    Priority (most "interesting" wins so the user sees actionable state
    first): pending_approval > running > failed > expired > done.
    `pending_approval` is split out from generic `pending` because the
    UI surfaces a click-to-approve button on those rows; the user's
    glance-and-skip pattern wants that signal at the top of the heap.
    """
    states = {c.status for c in commands}
    approval_states = {c.approval_state for c in commands}
    if "pending" in approval_states:
        return "pending_approval"
    if "running" in states or "claimed" in states:
        return "running"
    if "failed" in states:
        return "failed"
    if "expired" in states:
        return "expired"
    if "rejected" in approval_states:
        return "rejected"
    # Command.status uses "completed" (set by complete_command).
    if all(c.status == "completed" for c in commands):
        return "done"
    return "pending"


@app.get("/workspaces/me/dispatch")
def list_dispatch_chains(
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List dispatch chains, newest first.

    A "chain" is the set of commands sharing one `dispatch_chain_id` —
    the cause-and-effect tree that starts with a user click, scheduled
    tick, or webhook push, and fans out through agent-to-agent
    dispatches. The 11.6 dashboard view renders one row per chain,
    expandable into a timeline that this endpoint's siblings populate.
    """
    limit = max(1, min(limit, 200))
    # Find the most recently active chain ids for this workspace.
    chain_rows = session.execute(
        text(
            """
            SELECT dispatch_chain_id AS chain_id,
                   MIN(created_at)   AS started_at,
                   MAX(COALESCE(completed_at, claimed_at, created_at))
                                     AS last_activity_at
            FROM commands
            WHERE workspace_id = :wsid
            GROUP BY dispatch_chain_id
            ORDER BY last_activity_at DESC
            LIMIT :lim
            """
        ),
        {"wsid": workspace_id, "lim": limit},
    ).all()

    if not chain_rows:
        return {"chains": []}

    chain_ids = [r.chain_id for r in chain_rows]
    cmds = session.execute(
        select(Command)
        .where(
            Command.workspace_id == workspace_id,
            Command.dispatch_chain_id.in_(chain_ids),
        )
        .order_by(Command.dispatch_depth, Command.created_at)
    ).scalars().all()

    by_chain: dict[str, list[Command]] = {}
    for c in cmds:
        by_chain.setdefault(c.dispatch_chain_id, []).append(c)

    chains: list[dict[str, Any]] = []
    for r in chain_rows:
        chain_cmds = by_chain.get(r.chain_id, [])
        if not chain_cmds:
            continue
        root = chain_cmds[0]  # ordered by depth ASC, so depth=0 first
        pending_approvals = sum(
            1 for c in chain_cmds if c.approval_state == "pending"
        )
        chains.append(
            {
                "chain_id": r.chain_id,
                "started_at": r.started_at.isoformat(),
                "last_activity_at": r.last_activity_at.isoformat(),
                "command_count": len(chain_cmds),
                "max_depth": max(c.dispatch_depth for c in chain_cmds),
                "root_agent": root.agent_name,
                "root_kind": root.kind,
                "root_source_agent": root.source_agent,
                "status": _aggregate_chain_status(chain_cmds),
                "pending_approval_count": pending_approvals,
            }
        )
    return {"chains": chains}


@app.get("/workspaces/me/dispatch/{chain_id}")
def get_dispatch_chain(
    chain_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Full timeline for one dispatch chain.

    Returns every command in the chain (with the same shape as
    `/agents/{name}/commands`) plus any events whose payload carries a
    `command_id` matching one of the chain's commands. The frontend
    renders these together as a single vertical timeline indented by
    `dispatch_depth`.
    """
    cmds = session.execute(
        select(Command)
        .where(
            Command.workspace_id == workspace_id,
            Command.dispatch_chain_id == chain_id,
        )
        .order_by(Command.dispatch_depth, Command.created_at)
    ).scalars().all()
    if not cmds:
        raise HTTPException(status_code=404, detail="chain not found")

    cmd_ids = [c.id for c in cmds]
    # Events linked to these commands via payload.command_id. Atlas /
    # Hermes / Polaris all stamp this; events from agents that don't
    # are excluded — for 11.6 that's the right call (the chain view
    # shouldn't pull in arbitrary agent telemetry).
    events = session.execute(
        text(
            """
            SELECT id, workspace_id, run_id, agent_name, kind, payload, timestamp
            FROM events
            WHERE workspace_id = :wsid
              AND payload->>'command_id' = ANY(:cmd_ids)
            ORDER BY timestamp ASC
            """
        ),
        {"wsid": workspace_id, "cmd_ids": cmd_ids},
    ).all()

    return {
        "chain_id": chain_id,
        "commands": [_serialize_command(c) for c in cmds],
        "events": [
            {
                "id": e.id,
                "run_id": e.run_id,
                "agent_name": e.agent_name,
                "kind": e.kind,
                "payload": e.payload or {},
                "timestamp": e.timestamp.isoformat(),
                "command_id": (e.payload or {}).get("command_id"),
            }
            for e in events
        ],
        "status": _aggregate_chain_status(cmds),
    }


@app.post("/commands/{command_id}/complete")
def complete_command(
    command_id: str,
    body: CommandCompleteIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.status not in ("claimed", "pending"):
        raise HTTPException(status_code=400, detail=f"command already {cmd.status}")
    if body.error:
        cmd.status = "failed"
        cmd.error = body.error
    else:
        cmd.status = "completed"
        cmd.result = body.result
    cmd.completed_at = utcnow()
    session.flush()
    return _serialize_command(cmd)


@app.delete("/commands/{command_id}")
def cancel_command(
    command_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"can only cancel pending commands; this one is {cmd.status}",
        )
    cmd.status = "cancelled"
    cmd.completed_at = utcnow()
    session.flush()
    return _serialize_command(cmd)


def _serialize_manifest(a: Agent) -> dict[str, Any]:
    return {
        "agent_name": a.name,
        "command_handlers": a.command_handlers or [],
        "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
    }


@app.put("/agents/{agent_name}/manifest")
def put_manifest(
    agent_name: str,
    body: AgentManifestIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Replace an agent's command-handler manifest. Called by the SDK on
    init() once handlers are known. Also bumps last_seen_at."""
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=500, detail="agent ensure failed")
    a.command_handlers = body.command_handlers
    a.last_seen_at = now
    a.updated_at = now
    session.flush()
    return _serialize_manifest(a)


@app.get("/agents/{agent_name}/manifest")
def get_manifest(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        return {
            "agent_name": agent_name,
            "command_handlers": [],
            "last_seen_at": None,
        }
    return _serialize_manifest(a)


# ---------- Anthropic error translation ---------- #
#
# Both 12B.1 (generate one bot) and 12C.1 (plan a team) hit the same
# Anthropic API with forced tool_choice. When Anthropic returns 529
# (overloaded) the user gets a confusing 502; translate to a clear 503
# with retry guidance instead. Other API errors surface as 502 like
# before.

def _anthropic_error_to_http(exc: "Exception") -> HTTPException:
    status = getattr(exc, "status_code", None)
    if status == 529:
        return HTTPException(
            status_code=503,
            detail=(
                "Anthropic is overloaded right now (529). The SDK retried "
                "and gave up — try again in a few seconds."
            ),
        )
    if status == 429:
        return HTTPException(
            status_code=429,
            detail=(
                "Anthropic rate-limited this workspace's key. Slow down "
                "(or check your tier limits) and retry."
            ),
        )
    return HTTPException(status_code=502, detail=f"Anthropic API error: {exc}")


# ---------- Phase 12B.1: agent code generation ---------- #


@app.post("/workspaces/me/agents/generate", status_code=202)
def generate_agent(
    body: AgentGenerateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enqueue an agent-generation job and return its id.

    Phase 12C.6: the synchronous Anthropic call moved off the request
    path because it could exceed Railway's edge timeout (~100s with
    retries on Opus). The endpoint now validates input + pre-checks
    workspace state, then writes a `pending` row to `generation_jobs`
    and returns `{job_id, status: "pending"}`. The dashboard polls
    `GET /workspaces/me/generation-jobs/{id}` until terminal.

    Pre-checks that 4xx synchronously (not enqueued): no
    ANTHROPIC_API_KEY secret, or workspace already over its monthly
    budget. Anything that can only fail mid-LLM-call (rate limits,
    validation retry exhaustion, etc.) is recorded as `error` on the
    job row instead.
    """
    import jobs

    # 1. Auth-paired prereqs: workspace's Anthropic key + cost-cap room.
    secret_row = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY")
    )
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ANTHROPIC_API_KEY not set on this workspace. Add it on "
                "/account before using the bot generator."
            ),
        )

    # Cost-cap check: workspace.budget_usd_monthly is the cap; reject
    # if MTD spend already at or over. Generation calls are LLM calls
    # like any other and shouldn't bypass the budget gate.
    workspace = session.get(Workspace, workspace_id)
    if workspace and workspace.budget_usd_monthly is not None:
        cost = workspace_cost_mtd(session, workspace_id)
        used = float(cost.get("total_usd") or 0)
        cap = float(workspace.budget_usd_monthly)
        if cap > 0 and used >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"workspace MTD spend ${used:.2f} ≥ budget ${cap:.2f}; "
                    "bump the budget on /account to keep generating."
                ),
            )

    job_id = str(uuid.uuid4())
    jobs.enqueue_job(
        session,
        job_id=job_id,
        workspace_id=workspace_id,
        kind="agent_generate",
        request_payload=body.model_dump(exclude_none=False),
    )
    return {"job_id": job_id, "status": "pending"}


# ---------- Phase 12C.1: project-analysis endpoint ---------- #


@app.post("/workspaces/me/teams/plan", status_code=202)
def plan_team(
    body: TeamPlanIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enqueue a team-plan job and return its id.

    Phase 12C.6: the synchronous Anthropic call (and the GitHub README
    fetch) moved off the request path; both could push request time past
    Railway's ~100s edge timeout, especially when paired with a
    validation retry. The endpoint now validates input + pre-checks
    workspace state, then writes a `pending` row to `generation_jobs`
    and returns `{job_id, status: "pending"}`. The dashboard polls
    `GET /workspaces/me/generation-jobs/{id}` until terminal.

    Pre-checks that 4xx synchronously (not enqueued): missing project-info
    input, no ANTHROPIC_API_KEY secret, malformed github_repo, or
    workspace already over its monthly budget. Anything that can only
    fail mid-run (Anthropic errors, GitHub fetch failures, validation
    retry exhaustion) is recorded as `error` on the job row instead.
    """
    import jobs

    # 1. Input gate: at least one source of project info.
    if not (body.readme_text or body.freeform_description or body.github_repo):
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide at least one of `readme_text`, "
                "`freeform_description`, or `github_repo`."
            ),
        )

    # 2. Workspace's Anthropic key gate (same shape as /agents/generate).
    secret_row = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY")
    )
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ANTHROPIC_API_KEY not set on this workspace. Add it on "
                "/account before using the team planner."
            ),
        )

    # 3. Budget gate.
    workspace = session.get(Workspace, workspace_id)
    if workspace and workspace.budget_usd_monthly is not None:
        cost = workspace_cost_mtd(session, workspace_id)
        used = float(cost.get("total_usd") or 0)
        cap = float(workspace.budget_usd_monthly)
        if cap > 0 and used >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"workspace MTD spend ${used:.2f} ≥ budget ${cap:.2f}; "
                    "bump the budget on /account to keep planning."
                ),
            )

    # 4. github_repo format validation: pre-parse so the user sees
    # malformed-URL errors synchronously. The actual GitHub fetch
    # happens in the handler so a slow network call is off the request
    # path. We stash the parsed pair on the payload so the handler
    # doesn't re-parse.
    payload = body.model_dump(exclude_none=False)
    if body.github_repo and not body.readme_text:
        owner, name = _parse_github_repo(body.github_repo)
        payload["github_repo_parsed"] = [owner, name]

    job_id = str(uuid.uuid4())
    jobs.enqueue_job(
        session,
        job_id=job_id,
        workspace_id=workspace_id,
        kind="team_plan",
        request_payload=payload,
    )
    return {"job_id": job_id, "status": "pending"}


# ---------- Phase 12C.6.5: poll endpoint for generation_jobs ---------- #


@app.get("/workspaces/me/generation-jobs/{job_id}")
def get_generation_job(
    job_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Return the current state of a generation job.

    Dashboard polls this until `status` is terminal (`success` or
    `failed`). 404 covers both "no such row" and "row belongs to a
    different workspace" — we don't leak existence across workspaces.
    """
    row = session.get(GenerationJob, job_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "result_payload": row.result_payload,
        "error": row.error,
        "attempt_count": row.attempt_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _parse_github_repo(s: str) -> tuple[str, str]:
    """Accept `owner/name` or a full GitHub URL and return `(owner, name)`."""
    raw = s.strip()
    # Tolerate trailing `.git` (common for clone URLs).
    if raw.endswith(".git"):
        raw = raw[:-4]
    # https://github.com/owner/name or git@github.com:owner/name
    for prefix in ("https://github.com/", "http://github.com/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw.startswith("git@github.com:"):
        raw = raw[len("git@github.com:"):]
    parts = raw.strip("/").split("/")
    if len(parts) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"github_repo must be `owner/name` or a GitHub URL (got {s!r})",
        )
    return parts[0], parts[1]


def _serialize_instance(i: AgentInstance, now: datetime) -> dict[str, Any]:
    age = now - i.last_heartbeat_at
    return {
        "id": i.id,
        "agent_name": i.agent_name,
        "hostname": i.hostname,
        "pid": i.pid,
        "sdk_version": i.sdk_version,
        "started_at": i.started_at.isoformat(),
        "last_heartbeat_at": i.last_heartbeat_at.isoformat(),
        "status": "active" if age <= INSTANCE_ACTIVE_WINDOW else "stale",
    }


@app.post("/agents/{agent_name}/instances/heartbeat")
def instance_heartbeat(
    agent_name: str,
    body: InstanceHeartbeatIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Upsert by instance_id: register on first call, refresh last_heartbeat_at
    on every subsequent call. Idempotent. SDK calls this on init() and then
    on a timer.
    """
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    inst = session.get(AgentInstance, body.instance_id)
    if inst is None:
        # Hostname-scoped concurrency cap: only enforced on new
        # registrations (existing instances refreshing their own
        # heartbeat are not subject to it — they were registered
        # under a previous-or-current cap). Stale rows from
        # crashed processes don't count because the window filter
        # excludes them.
        if body.hostname:
            cutoff = now - INSTANCE_ACTIVE_WINDOW
            active_count = session.execute(
                select(func.count(AgentInstance.id))
                .where(AgentInstance.workspace_id == workspace_id)
                .where(AgentInstance.agent_name == agent_name)
                .where(AgentInstance.hostname == body.hostname)
                .where(AgentInstance.last_heartbeat_at >= cutoff)
            ).scalar_one()
            if active_count >= MAX_INSTANCES_PER_HOSTNAME:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"refused: {active_count} active instance(s) of "
                        f"'{agent_name}' already running on '{body.hostname}' "
                        f"(cap = {MAX_INSTANCES_PER_HOSTNAME}). Kill the "
                        "older processes (e.g. `pkill -f bot.py`) before "
                        "starting another, or raise the cap via "
                        "LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME."
                    ),
                )
        inst = AgentInstance(
            id=body.instance_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            hostname=body.hostname,
            pid=body.pid,
            sdk_version=body.sdk_version,
            started_at=body.started_at or now,
            last_heartbeat_at=now,
        )
        session.add(inst)
    elif inst.workspace_id != workspace_id or inst.agent_name != agent_name:
        # Instance id collision across workspaces or agents — refuse rather
        # than overwrite. SDK uses uuid4 so this is virtually impossible
        # without a deliberately spoofed id.
        raise HTTPException(
            status_code=409, detail="instance id belongs to another agent",
        )
    else:
        inst.last_heartbeat_at = now
        # Allow these to refresh in case the SDK updates between heartbeats.
        if body.hostname is not None:
            inst.hostname = body.hostname
        if body.pid is not None:
            inst.pid = body.pid
        if body.sdk_version is not None:
            inst.sdk_version = body.sdk_version
    session.flush()
    # Phase 16.3: echo the agent's current capability list back so the
    # SDK can refresh its cache on every heartbeat. Dashboard edits to
    # `capabilities` propagate within one heartbeat interval (default
    # 10s) — no separate fetch needed.
    response = _serialize_instance(inst, now)
    agent_row = session.get(Agent, (workspace_id, agent_name))
    if agent_row is not None:
        response["capabilities"] = list(agent_row.capabilities or [])
        response["sensitivity_level"] = agent_row.sensitivity_level
    return response


@app.get("/agents/{agent_name}/instances")
def list_instances(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List instances for this agent, newest heartbeat first. Each row carries
    a computed `status` of `active` or `stale` so the dashboard doesn't need
    to know the heartbeat threshold."""
    now = utcnow()
    rows = session.execute(
        select(AgentInstance)
        .where(
            AgentInstance.workspace_id == workspace_id,
            AgentInstance.agent_name == agent_name,
        )
        .order_by(desc(AgentInstance.last_heartbeat_at))
    ).scalars().all()
    return {"instances": [_serialize_instance(i, now) for i in rows]}


def _serialize_thread(t: Thread) -> dict[str, Any]:
    return {
        "id": t.id,
        "agent_name": t.agent_name,
        "title": t.title,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


def _serialize_thread_message(m: ThreadMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "thread_id": m.thread_id,
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "error": m.error,
        "created_at": m.created_at.isoformat(),
        "completed_at": m.completed_at.isoformat() if m.completed_at else None,
    }


def _thread_for_workspace(
    session: Session, thread_id: str, workspace_id: str
) -> Thread:
    t = session.get(Thread, thread_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="thread not found")
    return t


@app.post("/agents/{agent_name}/threads")
def create_thread(
    agent_name: str,
    body: ThreadCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    t = Thread(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        title=body.title or "New thread",
        created_at=now,
        updated_at=now,
    )
    session.add(t)
    session.flush()
    return _serialize_thread(t)


@app.get("/agents/{agent_name}/threads")
def list_threads(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(Thread)
        .where(
            Thread.workspace_id == workspace_id,
            Thread.agent_name == agent_name,
        )
        .order_by(desc(Thread.updated_at))
    ).scalars().all()
    return {"threads": [_serialize_thread(t) for t in rows]}


@app.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    t = _thread_for_workspace(session, thread_id, workspace_id)
    msgs = session.execute(
        select(ThreadMessage)
        .where(ThreadMessage.thread_id == t.id)
        .order_by(ThreadMessage.created_at)
    ).scalars().all()
    return {
        "thread": _serialize_thread(t),
        "messages": [_serialize_thread_message(m) for m in msgs],
    }


@app.delete("/threads/{thread_id}")
def delete_thread(
    thread_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    t = _thread_for_workspace(session, thread_id, workspace_id)
    session.delete(t)
    session.flush()
    return {"deleted": thread_id}


@app.post("/threads/{thread_id}/messages")
def post_thread_message(
    thread_id: str,
    body: ThreadMessagePostIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """User sends a message. Creates a 'user' completed message and a
    'pending' assistant message that the agent will claim."""
    t = _thread_for_workspace(session, thread_id, workspace_id)
    now = utcnow()
    user_msg = ThreadMessage(
        id=str(uuid.uuid4()),
        thread_id=t.id,
        role="user",
        content=body.content,
        status="completed",
        created_at=now,
        completed_at=now,
    )
    pending = ThreadMessage(
        id=str(uuid.uuid4()),
        thread_id=t.id,
        role="assistant",
        content="",
        status="pending",
        created_at=now,
    )
    session.add(user_msg)
    session.add(pending)
    t.updated_at = now
    # If the thread still has the default title, derive one from the first
    # user message — capped at 60 chars.
    if t.title == "New thread":
        t.title = body.content.strip().splitlines()[0][:60]
    session.flush()
    return {
        "user_message": _serialize_thread_message(user_msg),
        "pending_message": _serialize_thread_message(pending),
    }


@app.post("/agents/{agent_name}/threads/claim")
def claim_thread_turn(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Atomically claim the oldest pending assistant message across all
    threads for this agent in the caller's workspace. Returns the message
    id plus the full conversation history (completed messages only)."""
    now = utcnow()
    row = session.execute(
        text(
            """
            SELECT m.id AS message_id, m.thread_id AS thread_id
            FROM thread_messages m
            JOIN threads t ON t.id = m.thread_id
            WHERE t.workspace_id = :wsid
              AND t.agent_name = :agent
              AND m.status = 'pending'
              AND m.role = 'assistant'
            ORDER BY m.created_at ASC
            LIMIT 1
            FOR UPDATE OF m SKIP LOCKED
            """
        ),
        {"wsid": workspace_id, "agent": agent_name},
    ).first()
    if row is None:
        return {"turn": None}
    msg = session.get(ThreadMessage, row.message_id)
    if msg is None:
        return {"turn": None}
    # Mark as claimed by writing claimed_at? We use status transitions: we
    # leave status='pending' (no separate claimed state for messages —
    # callers are expected to either complete or fail). To prevent re-claim
    # during the same instant, FOR UPDATE plus the next claim's WHERE
    # status='pending' is enough; but we also bump it to 'in_progress' for
    # clarity.
    msg.status = "in_progress"
    history = session.execute(
        select(ThreadMessage)
        .where(
            ThreadMessage.thread_id == row.thread_id,
            ThreadMessage.status == "completed",
        )
        .order_by(ThreadMessage.created_at)
    ).scalars().all()
    messages = [{"role": m.role, "content": m.content} for m in history]

    # Prepend the agent's configured system prompt if set and not already
    # present at the start of the thread.
    agent_row = session.get(Agent, (workspace_id, agent_name))
    if (
        agent_row is not None
        and agent_row.system_prompt
        and not (messages and messages[0].get("role") == "system")
    ):
        messages = [
            {"role": "system", "content": agent_row.system_prompt},
            *messages,
        ]

    session.flush()
    return {
        "turn": {
            "message_id": msg.id,
            "thread_id": msg.thread_id,
            "messages": messages,
        }
    }


@app.post("/messages/{message_id}/chunk")
def append_thread_message_chunk(
    message_id: str,
    body: ThreadMessageChunkIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Append streaming output to an in-progress assistant message."""
    msg = session.get(ThreadMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    t = session.get(Thread, msg.thread_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    if msg.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400, detail=f"message already {msg.status}"
        )
    if msg.status == "pending":
        msg.status = "in_progress"
    msg.content = (msg.content or "") + body.delta
    t.updated_at = utcnow()
    session.flush()
    return _serialize_thread_message(msg)


@app.post("/messages/{message_id}/complete")
def complete_thread_message(
    message_id: str,
    body: ThreadMessageCompleteIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    msg = session.get(ThreadMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    t = session.get(Thread, msg.thread_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    if msg.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400,
            detail=f"message already {msg.status}",
        )
    now = utcnow()
    if body.error:
        msg.status = "failed"
        msg.error = body.error
    else:
        msg.status = "completed"
        # If the caller passed content, that wins (final authoritative). If
        # not, keep whatever we accumulated from chunks.
        if body.content is not None:
            msg.content = body.content
    msg.completed_at = now
    t.updated_at = now
    session.flush()
    return _serialize_thread_message(msg)


# ---------- Phase 30.3.c: team conversations (Polaris-routed) ---------- #
#
# Per-bot threads (above) are a 1:1 chat with one agent. Team
# conversations are 1:N: the operator addresses the whole team, the
# Polaris router (backend/team_router.py) picks the responding subset,
# and the dispatch step inserts one pending assistant row per pick.
# Each agent's existing claim loop (POST /agents/{name}/threads/claim)
# claims its own assistant row by agent_name on team_messages — the
# claim handler will need a sibling for team rows in a future task;
# 30.3.c only ships the operator-facing surface + router wiring.


def _serialize_team_conversation(c: TeamConversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "workspace_id": c.workspace_id,
        "title": c.title,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


def _serialize_team_message(m: TeamMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "agent_name": m.agent_name,
        "routed_agents": m.routed_agents,
        "error": m.error,
        "created_at": m.created_at.isoformat(),
        "completed_at": (
            m.completed_at.isoformat() if m.completed_at else None
        ),
    }


def _team_conv_for_workspace(
    session: Session, conversation_id: str, workspace_id: str,
) -> TeamConversation:
    c = session.get(TeamConversation, conversation_id)
    if c is None or c.workspace_id != workspace_id:
        raise HTTPException(
            status_code=404, detail="team conversation not found",
        )
    return c


@app.post("/workspaces/me/team-conversations")
def create_team_conversation(
    body: TeamConversationCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    now = utcnow()
    c = TeamConversation(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        title=body.title or "Team chat",
        created_at=now,
        updated_at=now,
    )
    session.add(c)
    session.flush()
    return _serialize_team_conversation(c)


@app.get("/workspaces/me/team-conversations")
def list_team_conversations(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(TeamConversation)
        .where(TeamConversation.workspace_id == workspace_id)
        .order_by(desc(TeamConversation.updated_at))
    ).scalars().all()
    return {"conversations": [_serialize_team_conversation(c) for c in rows]}


@app.get("/team-conversations/{conversation_id}")
def get_team_conversation(
    conversation_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    c = _team_conv_for_workspace(session, conversation_id, workspace_id)
    msgs = session.execute(
        select(TeamMessage)
        .where(TeamMessage.conversation_id == c.id)
        .order_by(TeamMessage.created_at)
    ).scalars().all()
    return {
        "conversation": _serialize_team_conversation(c),
        "messages": [_serialize_team_message(m) for m in msgs],
    }


@app.delete("/team-conversations/{conversation_id}")
def delete_team_conversation(
    conversation_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    c = _team_conv_for_workspace(session, conversation_id, workspace_id)
    session.delete(c)
    session.flush()
    return {"deleted": conversation_id}


# Test-injection point: tests override this with a factory that
# returns a fake Anthropic client. Default None means team_router uses
# its own anthropic.Anthropic() call.
_TEAM_ROUTER_ANTHROPIC_FACTORY: Optional[Any] = None


@app.post("/team-conversations/{conversation_id}/messages")
def post_team_conversation_message(
    conversation_id: str,
    body: TeamMessagePostIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Operator posts a message to the whole team.

    Writes the user row, runs the Polaris router, writes the router
    row + one pending assistant row per picked agent. Each agent's
    claim loop picks up its own row (claim handler matches on
    team_messages.agent_name).

    If the router fails (RouterError, e.g. missing ANTHROPIC_API_KEY),
    the user row still lands + a single router row is written with
    status='error' carrying the error text. No assistant rows are
    created in that case — the channel surfaces the failure rather
    than going silent.
    """
    import team_router  # local import: avoid module-load cycle risk
    c = _team_conv_for_workspace(session, conversation_id, workspace_id)
    now = utcnow()

    user_msg = TeamMessage(
        id=str(uuid.uuid4()),
        conversation_id=c.id,
        role="user",
        content=body.content,
        status="completed",
        created_at=now,
        completed_at=now,
    )
    session.add(user_msg)
    if c.title == "Team chat":
        c.title = body.content.strip().splitlines()[0][:60]

    # Run the router. team_router.route_team_message commits the
    # session before the LLM call (Railway idle-in-tx kill protection),
    # so the user_msg above is already durable when the call returns.
    router_id = str(uuid.uuid4())
    assistant_rows: list[TeamMessage] = []
    try:
        decision = team_router.route_team_message(
            session, workspace_id, body.content,
            anthropic_factory=_TEAM_ROUTER_ANTHROPIC_FACTORY,
        )
    except team_router.RouterError as e:
        router_row = TeamMessage(
            id=router_id,
            conversation_id=c.id,
            role="router",
            content=f"Router error: {e}",
            status="error",
            error=str(e),
            created_at=utcnow(),
            completed_at=utcnow(),
        )
        session.add(router_row)
        c.updated_at = utcnow()
        session.flush()
        return {
            "user_message": _serialize_team_message(user_msg),
            "router_message": _serialize_team_message(router_row),
            "pending_messages": [],
        }

    router_row = TeamMessage(
        id=router_id,
        conversation_id=c.id,
        role="router",
        content=decision.summary,
        status="completed",
        routed_agents=decision.as_routed_agents_json(),
        created_at=utcnow(),
        completed_at=utcnow(),
    )
    session.add(router_row)

    for pick in decision.agents:
        row = TeamMessage(
            id=str(uuid.uuid4()),
            conversation_id=c.id,
            role="assistant",
            agent_name=pick.name,
            status="pending",
            created_at=utcnow(),
        )
        session.add(row)
        assistant_rows.append(row)

    c.updated_at = utcnow()
    session.flush()
    return {
        "user_message": _serialize_team_message(user_msg),
        "router_message": _serialize_team_message(router_row),
        "pending_messages": [
            _serialize_team_message(r) for r in assistant_rows
        ],
    }


# ---------- Phase 30.3.e: claim/chunk/complete for team messages ---------- #
#
# Sibling of the threads claim loop above (10442+) but keyed on
# team_messages. Each deployed agent runs ONE long-poll loop that
# tries both endpoints (threads.claim + team-conversations.claim)
# round-robin; the SDK extension comes in a follow-up.
#
# History fed to the bot: the operator's user message + the router
# row's summary as a system note. Peer agents' completed replies are
# NOT included in this iteration — keep the surface minimal so the
# claim → reply → complete round-trip is provable on its own. Peer
# awareness ("hermes already said X") is a follow-up.


@app.post("/agents/{agent_name}/team-conversations/claim")
def claim_team_turn(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Atomically claim the oldest pending team_message for this
    agent in the caller's workspace. Returns the message id + the
    user-visible history (user row + router summary as a system
    note). Returns {"turn": null} if nothing is pending."""
    row = session.execute(
        text(
            """
            SELECT m.id AS message_id,
                   m.conversation_id AS conversation_id
            FROM team_messages m
            JOIN team_conversations c ON c.id = m.conversation_id
            WHERE c.workspace_id = :wsid
              AND m.agent_name = :agent
              AND m.status = 'pending'
              AND m.role = 'assistant'
            ORDER BY m.created_at ASC
            LIMIT 1
            FOR UPDATE OF m SKIP LOCKED
            """
        ),
        {"wsid": workspace_id, "agent": agent_name},
    ).first()
    if row is None:
        return {"turn": None}
    msg = session.get(TeamMessage, row.message_id)
    if msg is None:
        return {"turn": None}
    msg.status = "in_progress"

    history = session.execute(
        select(TeamMessage)
        .where(
            TeamMessage.conversation_id == row.conversation_id,
            TeamMessage.role.in_(("user", "router")),
        )
        .order_by(TeamMessage.created_at)
    ).scalars().all()
    messages: list[dict[str, str]] = []
    for h in history:
        if h.role == "user":
            messages.append({"role": "user", "content": h.content})
        elif h.role == "router":
            # Router rows carry the routing summary + structured
            # routed_agents JSON. The system note is what the bot
            # sees so it knows the channel context + why it was
            # picked (the routed_agents JSON is operator-facing,
            # not bot-facing).
            messages.append({
                "role": "system",
                "content": f"Polaris routing note: {h.content}",
            })

    # Prepend the agent's configured system prompt if any.
    agent_row = session.get(Agent, (workspace_id, agent_name))
    if (
        agent_row is not None
        and agent_row.system_prompt
        and not (messages and messages[0].get("role") == "system")
    ):
        messages = [
            {"role": "system", "content": agent_row.system_prompt},
            *messages,
        ]

    session.flush()
    return {
        "turn": {
            "message_id": msg.id,
            "conversation_id": msg.conversation_id,
            "messages": messages,
        }
    }


@app.post("/team-messages/{message_id}/chunk")
def append_team_message_chunk(
    message_id: str,
    body: ThreadMessageChunkIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Append streaming output to an in-progress assistant team
    message. Reuses ThreadMessageChunkIn (delta:str) since the
    payload is identical."""
    msg = session.get(TeamMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    conv = session.get(TeamConversation, msg.conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    if msg.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400, detail=f"message already {msg.status}"
        )
    if msg.status == "pending":
        msg.status = "in_progress"
    msg.content = (msg.content or "") + body.delta
    conv.updated_at = utcnow()
    session.flush()
    return _serialize_team_message(msg)


@app.post("/team-messages/{message_id}/complete")
def complete_team_message(
    message_id: str,
    body: ThreadMessageCompleteIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    msg = session.get(TeamMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    conv = session.get(TeamConversation, msg.conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    if msg.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400, detail=f"message already {msg.status}",
        )
    now = utcnow()
    if body.error:
        msg.status = "error"
        msg.error = body.error
    else:
        msg.status = "completed"
        if body.content is not None:
            msg.content = body.content
    msg.completed_at = now
    conv.updated_at = now
    session.flush()
    return _serialize_team_message(msg)


@app.get("/agents/{agent_name}/cost")
def get_agent_cost(
    agent_name: str,
    since: Optional[datetime] = Query(
        None, description="ISO 8601 UTC. Defaults to start of today UTC."
    ),
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    since_ts = since or utc_day_start()
    summary = agent_cost_since(session, workspace_id, agent_name, since_ts)
    return {
        "agent_name": agent_name,
        "since": since_ts.isoformat(),
        "as_of": utcnow().isoformat(),
        **summary,
    }
