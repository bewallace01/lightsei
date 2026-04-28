"""Output validators (Phase 7, guardrail layer 3).

Validators are pure functions: given a payload and a config dict, they
return a ValidationResult. No state, no I/O. They run server-side after
event ingestion to flag outputs that don't match expected schema or
content rules.

Phase 7A is advisory: results are stored alongside events but don't
block ingestion (see Phase 7.3 for the pipeline). Phase 7B will make
them pre-emit and blocking, which unlocks "act, don't just plan"
(Polaris dispatching commands).
"""
from typing import Any, Callable

from . import schema_strict
from ._types import ValidationResult, Violation


ValidatorFn = Callable[[Any, dict], ValidationResult]

# Registry of validator name -> implementation.
# Keep names stable: they're persisted in event_validations.validator_name
# and in workspace validator-config rows. Renaming a validator means
# migrating those rows.
REGISTRY: dict[str, ValidatorFn] = {
    "schema_strict": schema_strict.validate,
}


def validate(name: str, payload: Any, config: dict) -> ValidationResult:
    """Run the named validator against `payload` with `config`.

    Raises KeyError if the name isn't registered. Callers are expected
    to either own the registration (Polaris registers schema_strict for
    `polaris.plan` events at setup time) or to have already validated
    the name against REGISTRY.
    """
    fn = REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"no validator registered with name {name!r}")
    return fn(payload, config)


__all__ = ["REGISTRY", "ValidationResult", "ValidatorFn", "Violation", "validate"]
