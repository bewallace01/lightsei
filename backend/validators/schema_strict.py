"""Schema-strict validator: payload must match a JSON Schema.

Reports every violation found in the payload (not just the first), so
the dashboard can render a complete picture rather than a one-error-at-a-
time game of whack-a-mole. Uses jsonschema's Draft 2020-12 validator —
the same draft Anthropic's strict tool-use input schemas target, which
is what Polaris emits.

Pure function: no I/O, no side effects, safe to call with any payload.
"""
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from ._types import ValidationResult, Violation


def validate(payload: Any, config: dict) -> ValidationResult:
    """Validate `payload` against `config["schema"]`.

    Args:
        payload: the event payload (any JSON-serializable value).
        config: must contain a "schema" key with a JSON Schema document.

    Returns:
        {"ok": True, "violations": []} on a clean match, or
        {"ok": False, "violations": [...]} with one violation per
        schema error. Config errors (missing schema, malformed schema)
        are themselves reported as violations rather than raised — this
        keeps the validator pipeline crash-free even when an operator
        mis-registers a config.
    """
    schema = config.get("schema")
    if schema is None:
        return {
            "ok": False,
            "violations": [{
                "rule": "missing_config",
                "message": "schema-strict requires config['schema']",
            }],
        }

    # Validate the schema itself against the meta-schema first. This
    # catches things like "type: not_a_real_type" up front and gives
    # us a single coherent error message, rather than the validator
    # crashing mid-iter_errors with UnknownType.
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as e:
        return {
            "ok": False,
            "violations": [{
                "rule": "invalid_schema",
                "message": f"validator config schema is invalid: {e.message}",
            }],
        }

    validator = Draft202012Validator(schema)
    violations: list[Violation] = []
    try:
        for err in validator.iter_errors(payload):
            path = (
                "/" + "/".join(str(p) for p in err.absolute_path)
                if err.absolute_path
                else ""
            )
            v: Violation = {
                "rule": err.validator or "schema_violation",
                "message": err.message,
            }
            if path:
                v["path"] = path
            violations.append(v)
    except jsonschema.exceptions.SchemaError as e:
        # Schemas can pass meta-validation but still fail at evaluation
        # time (e.g. unresolvable $ref). Treat these the same as an
        # invalid schema so the pipeline doesn't crash.
        return {
            "ok": False,
            "violations": [{
                "rule": "invalid_schema",
                "message": f"schema evaluation failed: {e.message}",
            }],
        }

    return {"ok": len(violations) == 0, "violations": violations}
