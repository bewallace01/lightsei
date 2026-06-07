"""Guardrail layer 4: behavioral rules (streaming pattern detection).

Layer 2 (`policies/`) gates a single action before it happens. Layer 3
(`validation_pipeline.py`) validates a single output before delivery.
Layer 4, here, looks at the *pattern of a whole run*: is the agent stuck
in a loop, burning runaway tokens, or climbing from harmless actions to
dangerous ones?

This module is pure compute, mirroring `validation_pipeline.evaluate_validators`
and `eval_sampler`: it takes a run's events (as plain dicts) and a config,
and returns a list of `BehavioralViolation`. No DB, no I/O. The caller
(15.3) decides whether to record, surface, or eventually halt.

Each detector returns at most one violation (the worst instance it found),
so a run with three problems yields three violations, not three hundred.

Public surface:
  BehavioralViolation                     dataclass result
  BehaviorConfig                          thresholds (all overridable)
  detect_loop(events, *, config)
  detect_runaway_tokens(events, *, config)
  detect_escalating_permissions(events, *, config)
  evaluate_behavior(events, *, config)    runs all three
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

# An event is a plain dict with at least `kind` and `payload`; `agent_name`
# is used by the loop signature when present. The caller adapts Event rows
# (or live event dicts) into this shape.
Event = dict[str, Any]


@dataclass
class BehavioralViolation:
    rule: str          # "loop" | "runaway_tokens" | "escalating_permissions"
    severity: str      # "warn" | "block"
    reason: str
    details: dict[str, Any]


# Keys that vary between otherwise-identical actions; dropped before
# hashing a payload for loop detection so "the same plan again" collapses.
_VOLATILE_KEYS = frozenset(
    {"command_id", "run_id", "id", "timestamp", "ts", "at", "created_at",
     "duration_s", "request_id", "message_id", "chain_id", "dispatch_chain_id"}
)

# Substring -> sensitivity rank for escalating-permission detection. The
# action string is lowercased and matched by substring, so
# "connector:slack.send_message" -> "send" -> 3.
_DEFAULT_ACTION_RANKS: dict[str, int] = {
    "read": 1, "list": 1, "get": 1, "search": 1, "fetch": 1, "view": 1,
    "internet": 2, "http": 2, "browse": 2, "network": 2,
    "write": 3, "update": 3, "create": 3, "post": 3, "send": 3, "upload": 3,
    "delete": 4, "drop": 4, "remove": 4, "destroy": 4,
    "admin": 5, "grant": 5, "sudo": 5, "exec": 5, "deploy": 5, "rotate": 5,
}


@dataclass
class BehaviorConfig:
    # loop: same signature appearing >= loop_threshold times within the
    # last loop_window events.
    loop_threshold: int = 5
    loop_window: int = 20
    # runaway tokens: cumulative input+output (+cache) tokens in the run.
    token_cap: int = 200_000
    # escalation: a strictly-rising sensitivity reaching >= high_rank in
    # >= min_escalation_steps "new high" steps.
    high_rank: int = 4
    min_escalation_steps: int = 3
    action_ranks: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_ACTION_RANKS)
    )


# ---------- helpers ---------- #


def _signature(event: Event) -> str:
    """Stable signature for loop detection: (agent, kind, payload-sans-volatile)."""
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        stable = {k: v for k, v in payload.items() if k not in _VOLATILE_KEYS}
        try:
            payload_repr = json.dumps(stable, sort_keys=True, default=str)
        except Exception:
            payload_repr = repr(sorted(stable.items()))
    else:
        payload_repr = repr(payload)
    basis = f"{event.get('agent_name', '')}|{event.get('kind', '')}|{payload_repr}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _action_of(event: Event) -> str:
    payload = event.get("payload") or {}
    action = ""
    if isinstance(payload, dict):
        action = str(payload.get("action") or payload.get("tool") or "")
    return (action or str(event.get("kind", ""))).lower()


def _rank_of(action: str, ranks: dict[str, int]) -> Optional[int]:
    best: Optional[int] = None
    for kw, r in ranks.items():
        if kw in action:
            best = r if best is None else max(best, r)
    return best


# ---------- detectors ---------- #


def detect_loop(
    events: list[Event], *, config: BehaviorConfig = BehaviorConfig()
) -> Optional[BehavioralViolation]:
    """A run repeating the same (agent, kind, payload) past the threshold
    within the recent window is stuck. Returns the worst offender."""
    window = events[-config.loop_window:] if config.loop_window else events
    counts: dict[str, int] = {}
    for ev in window:
        sig = _signature(ev)
        counts[sig] = counts.get(sig, 0) + 1
    if not counts:
        return None
    worst_sig, worst_count = max(counts.items(), key=lambda kv: kv[1])
    if worst_count >= config.loop_threshold:
        sample = next(
            (e for e in window if _signature(e) == worst_sig), window[-1]
        )
        return BehavioralViolation(
            rule="loop",
            severity="warn",
            reason=(
                f"action repeated {worst_count} times in the last "
                f"{len(window)} events"
            ),
            details={
                "count": worst_count,
                "threshold": config.loop_threshold,
                "kind": sample.get("kind"),
                "agent_name": sample.get("agent_name"),
            },
        )
    return None


def detect_runaway_tokens(
    events: list[Event], *, config: BehaviorConfig = BehaviorConfig()
) -> Optional[BehavioralViolation]:
    """Cumulative LLM tokens across the run exceeding the cap."""
    total = 0
    for ev in events:
        if ev.get("kind") not in ("llm_call_completed", "llm_call_failed"):
            continue
        p = ev.get("payload") or {}
        if not isinstance(p, dict):
            continue
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            v = p.get(k)
            if isinstance(v, (int, float)):
                total += int(v)
    if total > config.token_cap:
        return BehavioralViolation(
            rule="runaway_tokens",
            severity="block",
            reason=f"run used {total} tokens, over the {config.token_cap} cap",
            details={"total_tokens": total, "cap": config.token_cap},
        )
    return None


def detect_escalating_permissions(
    events: list[Event], *, config: BehaviorConfig = BehaviorConfig()
) -> Optional[BehavioralViolation]:
    """A run that climbs from low-sensitivity actions to high-sensitivity
    ones in several steps (not just one high action in isolation)."""
    running_max = 0
    steps = 0
    path: list[int] = []
    for ev in events:
        r = _rank_of(_action_of(ev), config.action_ranks)
        if r is None:
            continue
        if r > running_max:
            running_max = r
            steps += 1
            path.append(r)
    if steps >= config.min_escalation_steps and running_max >= config.high_rank:
        return BehavioralViolation(
            rule="escalating_permissions",
            severity="block",
            reason=(
                f"actions escalated through {steps} rising sensitivity "
                f"levels up to rank {running_max}"
            ),
            details={
                "steps": steps,
                "max_rank": running_max,
                "high_rank": config.high_rank,
                "path": path,
            },
        )
    return None


def evaluate_behavior(
    events: list[Event], *, config: BehaviorConfig = BehaviorConfig()
) -> list[BehavioralViolation]:
    """Run all detectors over a run's events. Returns [] when clean."""
    out: list[BehavioralViolation] = []
    for detector in (
        detect_loop, detect_runaway_tokens, detect_escalating_permissions
    ):
        v = detector(events, config=config)
        if v is not None:
            out.append(v)
    return out
