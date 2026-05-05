"""Content-rules validator: regex / pattern checks against payload fields.

Each rule names a regex `pattern`, a list of field paths to check, and a
`mode` (`must_not_match` or `must_match`). The validator walks the
fields, runs the regex against any string values found, and emits a
violation per match (or per non-match, depending on mode).

Rules carry a `severity`: `fail` violations make the overall result
`ok=False`, `warn` violations are recorded but don't fail. This is the
hook that lets us register "soft" rules that only show up as advisory
chips on the dashboard without blocking ingestion.

Path syntax is intentionally minimal:
  - `summary`            -> payload["summary"]
  - `outer.inner`        -> nested dict access
  - `next_actions[].task` -> iterate the array, then access `task`
A real JSONPath library would handle wildcards / filters / unions; we
don't need any of that yet, and adding a dependency for it would be
larger than this whole module.

Pure function, no I/O.
"""
import re
from typing import Any, Iterator

from ._types import ValidationResult, Violation


# Default rule pack for the Phase 7 demo. Polaris registers this against
# `polaris.plan` events at deploy time (Phase 7.3 ships the registration).
# Operators can register a different rule pack per workspace; this is just
# the "out of the box" set the dogfood demo uses.
DEFAULT_RULE_PACK = [
    {
        "name": "email_in_summary",
        # Case-insensitive: "Alice@Example.COM" should fire the same as
        # the all-lowercase form. The `(?i)` inline flag keeps the rule
        # config a single string field — no need for a separate flags arg
        # in the rule schema.
        "pattern": r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}",
        "fields": ["summary"],
        "mode": "must_not_match",
        "severity": "fail",
    },
    {
        "name": "banned_destructive_verbs",
        # Case-insensitive: "Delete the cache" should fire as readily
        # as "delete the cache". `drop` was originally on this list but
        # produced too many false positives in normal English usage
        # (drop a file on /agents/new, drop a parking-lot item, drag-
        # and-drop UX, "drop" used as "remove from a small list"). The
        # other four verbs have unambiguously destructive valence in
        # software contexts; we keep them.
        "pattern": r"(?i)\b(delete|truncate|destroy|nuke)\b",
        "fields": ["next_actions[].task"],
        "mode": "must_not_match",
        "severity": "fail",
    },
]


def validate(payload: Any, config: dict) -> ValidationResult:
    rules = config.get("rules")
    if not isinstance(rules, list):
        return {
            "ok": False,
            "violations": [{
                "rule": "missing_config",
                "message": "content-rules requires config['rules'] to be a list",
            }],
        }

    violations: list[Violation] = []
    for rule in rules:
        violations.extend(_check_rule(payload, rule))

    has_fail = any(v.get("severity") == "fail" for v in violations)
    return {"ok": not has_fail, "violations": violations}


def _check_rule(payload: Any, rule: dict) -> Iterator[Violation]:
    name = rule.get("name", "<unnamed>")
    pattern = rule.get("pattern")
    fields = rule.get("fields", []) or []
    mode = rule.get("mode", "must_not_match")
    severity = rule.get("severity", "fail")

    if not pattern:
        yield {
            "rule": name,
            "severity": "fail",
            "message": "rule is missing 'pattern'",
        }
        return

    try:
        regex = re.compile(pattern)
    except re.error as e:
        yield {
            "rule": name,
            "severity": "fail",
            "message": f"rule pattern is not a valid regex: {e}",
        }
        return

    for field_path in fields:
        for value in _string_values_at_path(payload, field_path):
            match = regex.search(value)
            if mode == "must_not_match" and match:
                yield {
                    "rule": name,
                    "severity": severity,
                    "message": f"forbidden pattern matched in {field_path}",
                    "path": field_path,
                    "matched": _redact_match(match.group(0)),
                }
            elif mode == "must_match" and not match:
                yield {
                    "rule": name,
                    "severity": severity,
                    "message": f"required pattern did not match in {field_path}",
                    "path": field_path,
                }


def _redact_match(s: str) -> str:
    """Display-time redaction for matched substrings.

    Short matches (under 8 chars) are kept verbatim, since they're almost
    always the keyword the operator chose to flag (e.g. `delete`, `drop`).
    Longer matches are redacted to first-char + `***`, since they're more
    likely to be user-supplied content (emails, paths, names) the validator
    happened to catch. The full match still lives in the original event
    payload for anyone with the audit need; this field is for display.
    """
    if len(s) < 8:
        return s
    return s[0] + "***"


def _string_values_at_path(payload: Any, path: str) -> Iterator[str]:
    """Yield every string value found at `path` in `payload`.

    See module docstring for path syntax. Missing keys / wrong types
    silently yield nothing — they're a separate concern handled by
    schema-strict, not by content-rules.
    """
    parts = _parse_path(path)
    yield from _walk(payload, parts)


def _parse_path(path: str) -> list[str]:
    parts: list[str] = []
    for chunk in path.split("."):
        # Handle trailing []: "next_actions[]" -> "next_actions", "[]"
        # Repeat the strip in case of "outer[][]" (nested arrays).
        while chunk.endswith("[]"):
            base = chunk[:-2]
            if base:
                parts.append(base)
            parts.append("[]")
            chunk = ""
        if chunk:
            parts.append(chunk)
    return parts


def _walk(obj: Any, parts: list[str]) -> Iterator[str]:
    if not parts:
        if isinstance(obj, str):
            yield obj
        return
    head, rest = parts[0], parts[1:]
    if head == "[]":
        if isinstance(obj, list):
            for item in obj:
                yield from _walk(item, rest)
    else:
        if isinstance(obj, dict) and head in obj:
            yield from _walk(obj[head], rest)
