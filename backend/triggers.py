"""Phase 22.2: pure helpers for the triggers surface.

No FastAPI imports here. The CRUD endpoints in main.py call these to
parse + validate cron expressions, mint webhook tokens, and translate
the dashboard's friendly schedule presets to standard 5-field cron.

Why a separate module: the scheduler loop in worker/scheduler.py (22.3)
needs the same compute_next_run_at; keeping it here means the worker
doesn't pull in the FastAPI app graph.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Optional

from croniter import CroniterBadCronError, croniter


# Friendly presets the dashboard picker offers. Compiled to standard
# 5-field cron so the storage layer has one shape. Operators can also
# paste a raw expression via the "advanced cron" field, in which case
# the preset map is bypassed entirely.
_FRIENDLY_PRESETS: dict[str, str] = {
    # Every day at 9am.
    "daily": "0 9 * * *",
    # Weekdays at 9am (Mon-Fri).
    "weekdays": "0 9 * * 1-5",
    # Mondays at 9am.
    "weekly": "0 9 * * 1",
    # Top of every hour.
    "hourly": "0 * * * *",
}


def known_presets() -> list[str]:
    """Names the dashboard picker can offer. Sorted for stable UI."""
    return sorted(_FRIENDLY_PRESETS.keys())


def friendly_schedule_to_cron(preset: str) -> str:
    """Map a friendly preset name to a 5-field cron expression.

    Raises ValueError with a friendly message if the preset is unknown,
    so the API layer can 422 with the original input intact.
    """
    expr = _FRIENDLY_PRESETS.get(preset)
    if expr is None:
        raise ValueError(
            f"unknown schedule preset {preset!r}; "
            f"valid options: {', '.join(known_presets())}"
        )
    return expr


def validate_cron(schedule: str) -> None:
    """Raise ValueError with a friendly message if the cron expression
    is malformed. Returns None on valid input.

    Caller is responsible for surfacing the message at the API layer
    (e.g. as a 422 detail). Keeping the helper raises-only means the
    happy path is `validate_cron(s); use(s)` with no truthy check.
    """
    if not isinstance(schedule, str) or not schedule.strip():
        raise ValueError("cron expression must be a non-empty string")
    try:
        croniter(schedule.strip())
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


def compute_next_run_at(schedule: str, after: datetime) -> datetime:
    """Return the next datetime the schedule fires strictly after
    `after`. Assumes `after` is timezone-aware (UTC in our world);
    croniter preserves the tzinfo.

    Raises ValueError if the schedule is malformed. Callers that
    care about that case should validate_cron first.
    """
    if after.tzinfo is None:
        raise ValueError(
            "compute_next_run_at requires a timezone-aware datetime"
        )
    it = croniter(schedule.strip(), after)
    return it.get_next(datetime)


# Webhook token length. 32 bytes URL-safe random → ~43-char string.
# Long enough that brute-forcing is hopeless; short enough to fit on
# a single line for the operator to copy.
_WEBHOOK_TOKEN_BYTES = 32


def mint_webhook_token() -> tuple[str, str]:
    """Return (plaintext, sha256_hash). Plaintext is shown to the
    operator once (in the create-trigger modal) and never persisted;
    the hash is what lives in `triggers.webhook_token_hash` + what
    the public POST /triggers/{token}/fire endpoint compares against.

    Same plaintext-once shape as API keys (Phase 1) and Resend keys
    (Phase 17.2): rotate by deleting + recreating, never recover.
    """
    plaintext = secrets.token_urlsafe(_WEBHOOK_TOKEN_BYTES)
    digest = hash_webhook_token(plaintext)
    return plaintext, digest


def hash_webhook_token(plaintext: str) -> str:
    """sha256 the plaintext token. Pure function so the public webhook
    endpoint can hash the URL parameter without round-tripping through
    mint_webhook_token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def resolve_schedule(
    *, schedule: Optional[str], preset: Optional[str],
) -> str:
    """API helper: turn the create-body's (schedule, preset) pair into
    a single concrete cron expression. Exactly one of the two must be
    set; both unset or both set is a ValueError.

    Done here (not inline in the endpoint) so the same precedence logic
    is shared with future create paths (CLI, bulk-import, etc.).
    """
    has_sched = bool(schedule and schedule.strip())
    has_preset = bool(preset and preset.strip())
    if has_sched and has_preset:
        raise ValueError(
            "specify schedule OR preset, not both"
        )
    if not has_sched and not has_preset:
        raise ValueError(
            "specify either a cron schedule or a preset name"
        )
    if has_preset:
        return friendly_schedule_to_cron(preset.strip())
    assert schedule is not None  # narrow for type-checker
    validate_cron(schedule)
    return schedule.strip()
