"""Per-token pricing for known models.

Prices are USD per 1,000,000 tokens (in / out). Update as vendors adjust.
Unknown models cost zero, which is intentional: we don't want to silently
guess and then enforce a wrong cap.

The `PRICING` literal is the source of truth. Phase 11B.1 added a
`model_pricing` DB table that mirrors this dict so the dashboard can
render current rates without reaching back into the SDK source. The
table is re-asserted on every `upgrade_to_head()` (called from FastAPI's
startup hook) — see `seed_model_pricing` below. Cost computation in
`cost.py` reads from this dict directly to avoid a DB round-trip on
every event ingest.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

# Provider for each model — used to fill the `provider` column in the
# `model_pricing` table. Models not listed here fall back to "unknown",
# which is fine for the table mirror but doesn't affect cost computation.
PROVIDER_BY_PREFIX: list[tuple[str, str]] = [
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("claude-", "anthropic"),
]


def _provider_for(model: str) -> str:
    for prefix, provider in PROVIDER_BY_PREFIX:
        if model.startswith(prefix):
            return provider
    return "unknown"


# (input_per_million, output_per_million) in USD
PRICING: dict[str, tuple[float, float]] = {
    # OpenAI (https://openai.com/api/pricing/)
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-2024-11-20": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (1.10, 4.40),
    # Anthropic (https://anthropic.com/pricing). Numbers reflect historical
    # tier pricing. Verify against the current vendor page before relying on
    # these for tight caps.
    "claude-opus-4-7": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
}


def compute_cost_usd(
    model: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
) -> float:
    """Cost in USD for one LLM call. Missing fields are treated as 0."""
    if not model:
        return 0.0
    prices = PRICING.get(model)
    if prices is None:
        return 0.0
    in_per_m, out_per_m = prices
    in_tok = input_tokens or 0
    out_tok = output_tokens or 0
    return (in_tok * in_per_m + out_tok * out_per_m) / 1_000_000.0


def seed_model_pricing(session: Session) -> None:
    """Re-assert the `model_pricing` table from the `PRICING` literal.

    Called from FastAPI's startup hook after migrations apply. Idempotent:
    upserts each row, so re-running on every boot is safe and ensures the
    table tracks code changes without a manual UPDATE.

    Rows for models that have been removed from `PRICING` (vendor
    deprecation) are NOT deleted — they're left in place so historical
    runs that reference them keep their pricing context. Add a manual
    `DELETE FROM model_pricing WHERE model = 'xxx'` if you really want
    to drop one.
    """
    # Imported here (not at module level) so the SDK + tests that import
    # `pricing` for `compute_cost_usd` only don't pull in SQLAlchemy
    # models / the DB layer. Cheap, runs once at startup.
    from sqlalchemy import text as _sql_text

    now = datetime.now(timezone.utc)
    for model, (input_per_m, output_per_m) in PRICING.items():
        provider = _provider_for(model)
        session.execute(
            _sql_text(
                """
                INSERT INTO model_pricing
                  (provider, model, input_per_million_usd,
                   output_per_million_usd, effective_from, updated_at)
                VALUES
                  (:provider, :model, :in_per_m, :out_per_m, :now, :now)
                ON CONFLICT (provider, model) DO UPDATE SET
                  input_per_million_usd  = EXCLUDED.input_per_million_usd,
                  output_per_million_usd = EXCLUDED.output_per_million_usd,
                  updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "provider": provider,
                "model": model,
                "in_per_m": Decimal(str(input_per_m)),
                "out_per_m": Decimal(str(output_per_m)),
                "now": now,
            },
        )
    session.commit()
