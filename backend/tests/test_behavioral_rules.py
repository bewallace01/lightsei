"""Phase 15.1: behavioral rules (guardrail layer 4) — pure detectors.

`backend/behavioral_rules.py` is pure compute, so these tests need no DB:
they hand the detectors plain event dicts and assert the violations.
"""
from behavioral_rules import (
    BehaviorConfig,
    detect_escalating_permissions,
    detect_loop,
    detect_runaway_tokens,
    evaluate_behavior,
)


def _ev(kind, payload=None, agent="atlas"):
    return {"kind": kind, "agent_name": agent, "payload": payload or {}}


def _llm(in_tok=0, out_tok=0):
    return _ev("llm_call_completed", {"input_tokens": in_tok, "output_tokens": out_tok})


# ---------- detect_loop ---------- #


def test_loop_trips_at_threshold_ignoring_volatile_keys():
    # Same plan five times, each with a different command_id/timestamp.
    events = [
        _ev("polaris.plan", {"plan": "do the thing", "command_id": f"c{i}", "ts": i})
        for i in range(5)
    ]
    v = detect_loop(events)
    assert v is not None
    assert v.rule == "loop" and v.severity == "warn"
    assert v.details["count"] == 5
    assert v.details["kind"] == "polaris.plan"


def test_loop_below_threshold_is_clean():
    events = [_ev("polaris.plan", {"plan": "x", "command_id": f"c{i}"}) for i in range(4)]
    assert detect_loop(events) is None


def test_loop_distinct_actions_do_not_trip():
    events = [_ev("polaris.plan", {"plan": f"plan-{i}"}) for i in range(8)]
    assert detect_loop(events) is None


def test_loop_window_excludes_old_repeats():
    cfg = BehaviorConfig(loop_window=2, loop_threshold=2)
    # A repeated early, but the last 2 events are distinct.
    events = [_ev("k", {"a": 1}), _ev("k", {"a": 1}), _ev("k", {"a": 2})]
    assert detect_loop(events, config=cfg) is None
    # Widen the window to 3 and the early repeat is back in view.
    assert detect_loop(events, config=BehaviorConfig(loop_window=3, loop_threshold=2)) is not None


# ---------- detect_runaway_tokens ---------- #


def test_runaway_tokens_over_cap_blocks():
    events = [_llm(100_000, 0), _llm(100_000, 5_000), _llm(10_000, 0)]
    v = detect_runaway_tokens(events)
    assert v is not None
    assert v.rule == "runaway_tokens" and v.severity == "block"
    assert v.details["total_tokens"] == 215_000


def test_runaway_tokens_under_cap_clean():
    assert detect_runaway_tokens([_llm(50_000, 1_000)]) is None


def test_runaway_tokens_ignores_non_llm_events():
    # Token-looking payloads on non-llm events must not count.
    events = [_ev("polaris.plan", {"input_tokens": 999_999})]
    assert detect_runaway_tokens(events) is None


def test_runaway_tokens_custom_cap():
    assert detect_runaway_tokens([_llm(60, 50)], config=BehaviorConfig(token_cap=100)) is not None


# ---------- detect_escalating_permissions ---------- #


def test_escalation_rising_to_high_blocks():
    events = [
        _ev("act", {"action": "read"}),
        _ev("act", {"action": "files.write"}),
        _ev("act", {"action": "files.delete"}),
    ]
    v = detect_escalating_permissions(events)
    assert v is not None
    assert v.rule == "escalating_permissions" and v.severity == "block"
    assert v.details["steps"] == 3
    assert v.details["max_rank"] == 4


def test_escalation_single_high_action_is_clean():
    assert detect_escalating_permissions([_ev("act", {"action": "delete"})]) is None


def test_escalation_repeated_high_without_climb_is_clean():
    events = [_ev("act", {"action": "delete"}) for _ in range(4)]
    assert detect_escalating_permissions(events) is None


def test_escalation_climb_not_reaching_high_is_clean():
    # read -> internet -> write tops out at rank 3, below high_rank 4.
    events = [
        _ev("act", {"action": "read"}),
        _ev("act", {"action": "internet"}),
        _ev("act", {"action": "write"}),
    ]
    assert detect_escalating_permissions(events) is None


# ---------- evaluate_behavior ---------- #


def test_evaluate_returns_all_applicable_violations():
    events = (
        [_ev("polaris.plan", {"plan": "same", "command_id": f"c{i}"}) for i in range(5)]
        + [_llm(150_000, 100_000)]
        + [_ev("act", {"action": "read"}), _ev("act", {"action": "write"}), _ev("act", {"action": "admin"})]
    )
    rules = {v.rule for v in evaluate_behavior(events)}
    assert rules == {"loop", "runaway_tokens", "escalating_permissions"}


def test_evaluate_clean_run_is_empty():
    events = [_ev("polaris.plan", {"plan": f"p{i}"}) for i in range(3)] + [_llm(100, 50)]
    assert evaluate_behavior(events) == []
