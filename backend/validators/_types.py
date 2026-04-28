"""Shared types for the validators package.

A `Violation` is a free-form dict carrying at minimum a `rule` and a
`message`. Each validator is free to attach extra fields:

  - schema-strict adds `path` (JSON pointer to the offending field).
  - content-rules adds `matched` (the matched substring) and
    `severity` (`fail` | `warn`).

Plain dicts (rather than dataclasses or pydantic models) keep the
return type trivially JSON-serializable for storage in event_validations.
"""
from typing import Any, TypedDict


Violation = dict[str, Any]


class ValidationResult(TypedDict):
    ok: bool
    violations: list[Violation]
