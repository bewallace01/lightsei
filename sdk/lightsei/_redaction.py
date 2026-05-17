"""Phase 16.5: SDK-side redaction primitives.

Built-in detectors for the four shapes the canonical CRM-bot
scenario cares about: email, US phone, SSN (hyphenated), credit
card (Luhn-validated). Pluggable via `register_redactor(name, fn)`
so workspaces with niche PII shapes (internal employee ids,
account numbers, etc.) can extend without a SDK release.

Auto-applied to outgoing event payloads + dispatched command
payloads + chat-message body when the agent's `sensitivity_level`
is `'pii'`. Per-call opt-out via `redact=False` on emit /
send_command for the operator who genuinely needs the raw value
(e.g. an audit-trail bot whose whole job is preserving exact
input).

Recursion: payloads are dict / list / str / number trees. The
recursive helper redacts str leaves and walks dict + list
containers; numbers / bools / None pass through unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional


# Detector signature: takes a string, returns the string with
# shape-matching substrings replaced by a placeholder like
# `[redacted-email]`. A detector that finds nothing returns the
# string unchanged.
Detector = Callable[[str], str]


# ---------- Built-in detectors ---------- #


# Standard local@domain.tld shape. Conservative — won't match
# obfuscated emails like "alice at example dot com" or single-word
# local parts that lack a dot in the domain. Better to miss those
# than to over-redact common nouns.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
)


def _redact_email(text: str) -> str:
    return _EMAIL_RE.sub("[redacted-email]", text)


# US-style 10-digit phone with optional +1 / 1 prefix; accepts
# spaces, dots, hyphens, or parens between groups. Doesn't match
# arbitrary 10-digit runs (would catch dates, ids) — requires at
# least one separator OR a leading +/parens to look phone-shaped.
_PHONE_RE = re.compile(
    r"""(?x)
    (?:\+?1[\s.\-]?)?            # optional country code
    (?:
        \(\d{3}\)\s?\d{3}[\s.\-]?\d{4}  # (123) 456-7890
        |
        \d{3}[\s.\-]\d{3}[\s.\-]\d{4}   # 123-456-7890 / 123.456.7890
    )
    """,
)


def _redact_phone(text: str) -> str:
    return _PHONE_RE.sub("[redacted-phone]", text)


# SSN: 9 digits in the standard `XXX-XX-XXXX` hyphenated form.
# Pure 9-digit runs (without hyphens) are deliberately NOT matched
# — too many false positives (zip+4 codes, order numbers, etc.).
# Users who need broader SSN catching can register a custom
# detector with their tolerance for false positives.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _redact_ssn(text: str) -> str:
    return _SSN_RE.sub("[redacted-ssn]", text)


# Credit-card-shape: 13-19 digit runs with optional separators,
# Luhn-validated. Luhn rules out roughly 90% of random digit
# sequences, dramatically reducing false-positive risk vs. a pure
# 13-19-digit regex.
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[\s\-]?){12,18}\d\b")


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn algorithm. Input is the digits-only string."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_credit_card(text: str) -> str:
    def _sub(m: re.Match) -> str:
        digits = re.sub(r"[\s\-]", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return "[redacted-card]"
        return m.group()

    return _CARD_CANDIDATE_RE.sub(_sub, text)


BUILTIN_DETECTORS: Dict[str, Detector] = {
    "email": _redact_email,
    "phone": _redact_phone,
    "ssn": _redact_ssn,
    "credit_card": _redact_credit_card,
}

# User-registered detectors. Looked up after the built-ins so a
# `register_redactor('email', ...)` override takes precedence over
# the built-in implementation. (Same name in either map wins from
# the custom map.)
_CUSTOM_DETECTORS: Dict[str, Detector] = {}


def register_redactor(name: str, fn: Detector) -> None:
    """Register a custom detector that runs in addition to the
    built-ins. Same `name` as a built-in replaces the built-in for
    that detector's invocation, useful when a workspace wants
    tighter or looser email matching, etc."""
    if not isinstance(name, str) or not name:
        raise ValueError("register_redactor: name must be a non-empty string")
    if not callable(fn):
        raise ValueError("register_redactor: fn must be callable")
    _CUSTOM_DETECTORS[name] = fn


def _reset_custom_redactors_for_tests() -> None:
    """Wipe the custom-detector map. Called from
    `_client._reset_for_tests` to keep test isolation."""
    _CUSTOM_DETECTORS.clear()


def _active_detectors(
    detectors: Optional[list[str]] = None,
) -> Dict[str, Detector]:
    """Merge built-ins + custom, with custom winning on name
    collision. When `detectors` is None we run them all; when set,
    only the named ones run (lets a caller redact emails-only,
    etc.)."""
    merged: Dict[str, Detector] = {**BUILTIN_DETECTORS, **_CUSTOM_DETECTORS}
    if detectors is None:
        return merged
    return {name: merged[name] for name in detectors if name in merged}


def redact(text: str, *, detectors: Optional[list[str]] = None) -> str:
    """Apply every active detector in turn. Non-string input is
    returned unchanged (so callers can pass `redact(x)` without a
    type guard when `x` might be None or a number)."""
    if not isinstance(text, str):
        return text  # type: ignore[return-value]
    out = text
    for fn in _active_detectors(detectors).values():
        out = fn(out)
    return out


def redact_payload(
    value: Any, *, detectors: Optional[list[str]] = None,
) -> Any:
    """Recursive variant for event / command payloads. Dict + list
    containers are walked; str leaves get redacted; everything else
    (int, float, bool, None) passes through unchanged.

    Returns a NEW container (doesn't mutate the input) so callers
    can safely keep the original around for their own logging."""
    if isinstance(value, str):
        return redact(value, detectors=detectors)
    if isinstance(value, dict):
        return {
            k: redact_payload(v, detectors=detectors) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(v, detectors=detectors) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(v, detectors=detectors) for v in value)
    return value
