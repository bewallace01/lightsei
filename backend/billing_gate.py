"""Phase 17.5: paywall middleware.

Pure module: two helpers the LLM-calling handlers + the cost-write
path call into. No HTTP, no Stripe — just reads + writes the columns
Phase 17.1 added to the `workspaces` table.

Two surfaces:

- `assert_billing_active(session, workspace_id)` — call BEFORE
  spending. Raises `HTTPException(402)` when the workspace is on
  the free tier AND `free_credits_remaining_usd <= 0`. Paid-tier
  workspaces fly through here (still subject to the existing
  `budget_usd_monthly` cap from Phase 11B.1, which a separate
  helper handles). Free workspaces with credits remaining also fly
  through.

- `decrement_free_credits(session, workspace_id, amount_usd)` —
  call AFTER spending. Subtracts `amount_usd` from
  `free_credits_remaining_usd`, floored at 0 (don't let it go
  negative). No-op on paid-tier workspaces (their cost gets
  tracked via Stripe usage records / subscription billing, not
  the free-credit pool).

Centralized so 17.5's wiring sites all behave identically + tests
can monkeypatch a single function rather than fishing through
handlers.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Workspace


_ZERO = Decimal("0")


def assert_billing_active(session: Session, workspace_id: str) -> None:
    """Raise HTTPException(402) when the workspace can't spend.

    402 chosen deliberately (Payment Required) so client code can
    distinguish "out of credits" from 4xx-validation or
    5xx-server-error categories. Detail body matches the shape the
    dashboard's billing UI (17.7) renders: error code +
    remaining-credits + upgrade-URL hint."""
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        # Workspace missing is a deeper bug than billing — let the
        # caller's existing 404 / 500 surface it.
        return
    if ws.plan_tier == "paid":
        return  # paid tier flies through this gate; budget cap is separate
    remaining = ws.free_credits_remaining_usd or _ZERO
    if remaining > _ZERO:
        return
    raise HTTPException(
        status_code=402,
        detail={
            "error": "out_of_credits",
            "remaining_usd": float(remaining),
            "upgrade_url": "/account#billing",
            "message": (
                "Your free credits are exhausted. Upgrade to the "
                "$50/mo plan from /account#billing to keep spending."
            ),
        },
    )


def decrement_free_credits(
    session: Session,
    workspace_id: str,
    amount_usd: Any,
) -> None:
    """Subtract `amount_usd` from the workspace's free credit pool.

    No-op on paid-tier workspaces (Stripe handles their accounting).
    No-op on missing workspace (don't crash the cost-write path on a
    workspace that got deleted mid-flight).

    Floors the result at 0 so a tiny over-spend at the moment of
    exhaustion doesn't leave a negative balance that the paywall
    has to special-case."""
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.plan_tier == "paid":
        return
    if not isinstance(amount_usd, Decimal):
        # Be liberal in what we accept — callers might pass float
        # or string. The Decimal conversion keeps the arithmetic
        # precise to 6 decimal places (cost_usd column scale).
        amount_usd = Decimal(format(float(amount_usd), ".6f"))
    if amount_usd <= _ZERO:
        return
    new_balance = (ws.free_credits_remaining_usd or _ZERO) - amount_usd
    if new_balance < _ZERO:
        new_balance = _ZERO
    ws.free_credits_remaining_usd = new_balance
