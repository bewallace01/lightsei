"""Phase 9.2: per-platform formatter shape + dispatcher routing.

These tests exercise the formatters and the HTTP-out helper directly;
no FastAPI test client, no DB. The endpoint integration tests live in
test_notifications.py.

Each formatter gets one happy-path snapshot per trigger to lock in
the platform-native JSON shape (Block Kit / embed / Adaptive Card).
The post() side gets covered by patching httpx so we can exercise
2xx / 4xx / timeout paths without touching the network.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

import notifications
from notifications import REGISTRY, Signal, dispatch
from notifications import _http as notifications_http
from notifications._http import post_json
from notifications._shared import (
    first_violation_summary,
    relative_time,
    run_failed_summary,
    top_next_actions,
    truncate,
)


@contextmanager
def mock_httpx_post(handler):
    """Patch httpx.Client used inside notifications._http with one that
    routes through the given handler (a function taking a Request and
    returning a Response, or raising)."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        # Drop any incoming `transport` kwarg to avoid clashing.
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    with patch.object(notifications_http.httpx, "Client", side_effect=factory):
        yield


def _signal(trigger: str, **overrides) -> Signal:
    base = dict(
        trigger=trigger,
        agent_name="polaris",
        dashboard_url="https://app.lightsei.com/polaris",
        timestamp=datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc),
        payload={
            "summary": "Phase 9 mid-flight; notifications dispatcher just landed.",
            "next_actions": [
                {"task": "ship 9.4 trigger pipeline", "why": "BackgroundTasks hook into POST /events", "blocked_by": None},
                {"task": "build dashboard panel", "why": "lets users register channels", "blocked_by": "9.4 first"},
                {"task": "Phase 9 demo", "why": "Slack + Discord + Teams all firing", "blocked_by": "9.5"},
            ],
            "validations": [{
                "validator": "schema_strict",
                "status": "fail",
                "violations": [{
                    "rule": "required",
                    "message": "'summary' is a required property",
                    "matched": "",
                }],
            }],
            "error": "RuntimeError: pretend the bot crashed",
        },
        workspace_id="ws_test",
    )
    base.update(overrides)
    return Signal(**base)


# ---------- registry ---------- #


def test_registry_contains_v1_native_chat_types():
    """Pin the v1 channel-type set. webhook lands in 9.3."""
    assert sorted(REGISTRY) == ["discord", "mattermost", "slack", "teams"]


def test_dispatch_unknown_type_returns_failed_delivery():
    """Unknown type doesn't raise — it produces a `failed` delivery
    with a clear `unknown_channel_type` error so the audit trail
    captures the misconfiguration."""
    result = dispatch(
        channel_type="aim",  # ha
        target_url="https://example.com",
        signal=_signal("test"),
    )
    assert result.status == "failed"
    assert result.response_summary["error"] == "unknown_channel_type"
    assert "aim" in result.response_summary["message"]


# ---------- _shared helpers ---------- #


def test_relative_time_buckets():
    now = datetime.now(timezone.utc)
    from datetime import timedelta as td
    assert relative_time(now) == "just now"
    assert relative_time(now - td(minutes=5)) == "5m ago"
    assert relative_time(now - td(hours=3)) == "3h ago"
    assert relative_time(now - td(days=2)) == "2d ago"


def test_truncate_clamps_with_ellipsis():
    assert truncate("short", 10) == "short"
    assert truncate("a" * 300) == ("a" * 279) + "…"
    # Whitespace-only / None tolerated
    assert truncate("") == ""


def test_top_next_actions_clips_and_handles_garbage():
    payload = {"next_actions": [
        {"task": "a", "why": "b", "blocked_by": None},
        {"task": "c", "why": "d", "blocked_by": "x"},
        {"task": "e", "why": "f", "blocked_by": None},
        {"task": "g", "why": "h", "blocked_by": None},
        "not a dict, ignore me",  # mixed garbage tolerated
    ]}
    out = top_next_actions(payload, n=3)
    assert len(out) == 3
    assert out[0]["task"] == "a"
    assert out[1]["blocked_by"] == "x"


def test_first_violation_summary_returns_first_failing():
    payload = {"validations": [
        {"validator": "content_rules", "status": "pass", "violations": []},
        {"validator": "schema_strict", "status": "fail", "violations": [
            {"rule": "required", "message": "missing summary"},
        ]},
    ]}
    v = first_violation_summary(payload)
    assert v["validator"] == "schema_strict"
    assert v["rule"] == "required"


def test_first_violation_summary_no_failures_returns_placeholders():
    """Defensive: if the dispatcher fires on validation.fail but the
    payload doesn't actually have any fail-status validations (race
    condition possible), we don't raise."""
    v = first_violation_summary({"validations": [{"validator": "x", "status": "pass", "violations": []}]})
    assert v == {"validator": "?", "rule": "?", "matched": "", "message": ""}


def test_run_failed_summary_falls_back_through_field_names():
    assert run_failed_summary({"error": "oh no"})["error"] == "oh no"
    assert run_failed_summary({"error_message": "huh"})["error"] == "huh"
    assert run_failed_summary({})["error"] == ""


# ---------- Slack formatter ---------- #


def test_slack_format_polaris_plan_shape():
    body = notifications.slack.format(_signal("polaris.plan"))
    # Always includes a fallback `text` for screen readers / phone
    # previews, plus `blocks` for rich rendering.
    assert "text" in body and "blocks" in body
    assert "Polaris plan" in body["text"]
    block_types = [b["type"] for b in body["blocks"]]
    assert block_types[0] == "header"
    assert "section" in block_types
    assert block_types[-1] == "context"  # the "View ↗" footer


def test_slack_format_validation_fail_shape():
    body = notifications.slack.format(_signal("validation.fail"))
    assert "Validation failed" in body["text"]
    # FactSet-equivalent: a section with `fields` for validator + rule
    field_sections = [b for b in body["blocks"] if "fields" in b]
    assert field_sections, "expected a fields-bearing section"
    fields_text = " ".join(f["text"] for f in field_sections[0]["fields"])
    assert "Validator" in fields_text
    assert "Rule" in fields_text


def test_slack_format_run_failed_shape():
    body = notifications.slack.format(_signal("run_failed"))
    assert "run failed" in body["text"]
    full = " ".join(b.get("text", {}).get("text", "") if isinstance(b.get("text"), dict) else "" for b in body["blocks"])
    assert "RuntimeError" in full


def test_slack_format_test_message_shape():
    body = notifications.slack.format(_signal("test"))
    assert "test message" in body["text"].lower()


def test_slack_format_plan_with_missing_payload_doesnt_raise():
    """Defensive: a malformed polaris.plan event (Phase 7 surfaced
    these — empty payload, wrong types) shouldn't crash the
    formatter."""
    body = notifications.slack.format(_signal("polaris.plan", payload={}))
    assert "blocks" in body
    full = str(body)
    assert "no summary" in full or "(no summary" in full


# ---------- Discord formatter ---------- #


def test_discord_format_uses_embeds_with_color():
    body = notifications.discord.format(_signal("polaris.plan"))
    assert "embeds" in body and len(body["embeds"]) == 1
    embed = body["embeds"][0]
    assert "color" in embed
    # green for plan
    assert embed["color"] == 0x10B981


def test_discord_validation_fail_uses_red():
    body = notifications.discord.format(_signal("validation.fail"))
    assert body["embeds"][0]["color"] == 0xEF4444


def test_discord_run_failed_uses_red():
    body = notifications.discord.format(_signal("run_failed"))
    assert body["embeds"][0]["color"] == 0xEF4444


def test_discord_embed_includes_url_and_timestamp():
    body = notifications.discord.format(_signal("polaris.plan"))
    embed = body["embeds"][0]
    assert embed["url"] == "https://app.lightsei.com/polaris"
    assert embed["timestamp"]  # ISO string; format-checked by Discord


# ---------- Teams formatter ---------- #


def test_teams_format_wraps_in_attachment_envelope():
    body = notifications.teams.format(_signal("polaris.plan"))
    # Bot Framework message envelope is required by the modern Teams
    # Workflows webhook trigger.
    assert body["type"] == "message"
    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["contentUrl"] is None
    card = att["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.5"


def test_teams_card_includes_view_action():
    """Action.OpenUrl is the deep-link rendering on Teams. Must be
    present so the user can tap through to the dashboard."""
    body = notifications.teams.format(_signal("polaris.plan"))
    card = body["attachments"][0]["content"]
    assert "actions" in card
    open_urls = [a for a in card["actions"] if a["type"] == "Action.OpenUrl"]
    assert open_urls, "expected at least one Action.OpenUrl"
    assert open_urls[0]["url"] == "https://app.lightsei.com/polaris"


def test_teams_validation_fail_card_uses_attention_color():
    """Teams' Adaptive Cards color-code via the `color` enum on
    TextBlocks rather than embed-level colors — Attention = red."""
    body = notifications.teams.format(_signal("validation.fail"))
    card = body["attachments"][0]["content"]
    title_block = card["body"][0]
    assert title_block["color"] == "Attention"


def test_teams_run_failed_uses_monospace_for_error():
    body = notifications.teams.format(_signal("run_failed"))
    card = body["attachments"][0]["content"]
    monospace_blocks = [b for b in card["body"] if b.get("fontType") == "Monospace"]
    assert monospace_blocks, "expected the error block to use Monospace fontType"


# ---------- Mattermost ---------- #


def test_mattermost_format_is_slack_compat():
    """Mattermost reuses the Slack formatter via the registry. Both
    types should produce identical bodies for the same signal."""
    sig = _signal("polaris.plan")
    slack_body = notifications.slack.format(sig)
    mm_format = REGISTRY["mattermost"][0]
    mm_body = mm_format(sig)
    assert mm_body == slack_body


# ---------- HTTP-out (the post() shared path) ---------- #


def test_post_json_2xx_returns_sent():
    """A 2xx response from the receiver → status='sent', http_status
    captured in response_summary for the audit trail."""
    with mock_httpx_post(lambda req: httpx.Response(200, json={"ok": True})):
        delivery = post_json(url="https://example.com/hook", body={"text": "hi"})
    assert delivery.status == "sent"
    assert delivery.response_summary["http_status"] == 200


def test_post_json_4xx_returns_failed_with_status_code():
    """Slack's standard response for an invalid token is 4xx with a
    plain-text body. We capture both for debugging."""
    with mock_httpx_post(lambda req: httpx.Response(403, text="invalid_token")):
        delivery = post_json(
            url="https://hooks.slack.com/services/X/Y/Z", body={"text": "hi"},
        )
    assert delivery.status == "failed"
    assert delivery.response_summary["error"] == "http_error"
    assert delivery.response_summary["http_status"] == 403
    assert "invalid_token" in delivery.response_summary["response_preview"]


def test_post_json_timeout_returns_failed_cleanly():
    """A network timeout produces status='failed' with a clear
    error, not a raised exception."""
    def raise_timeout(req):
        raise httpx.TimeoutException("timed out", request=req)

    with mock_httpx_post(raise_timeout):
        delivery = post_json(url="https://example.com/hook", body={"text": "hi"})
    assert delivery.status == "failed"
    assert delivery.response_summary["error"] == "timeout"


def test_post_json_response_body_clipped():
    """Long response bodies get clipped at RESPONSE_BODY_PREVIEW_CHARS
    so a chatty receiver can't bloat the audit table."""
    with mock_httpx_post(lambda req: httpx.Response(200, text="A" * 5000)):
        delivery = post_json(url="https://example.com/hook", body={"text": "hi"})
    assert delivery.status == "sent"
    assert len(delivery.response_summary["response_preview"]) <= 500


# ---------- dispatch routing ---------- #


def test_dispatch_routes_by_type_and_returns_delivery():
    """End-to-end test of dispatch() — a real Signal + format step,
    HTTP mocked. Verifies the registry routes to the right formatter
    and the post step's outcome ends up on the Delivery."""
    captured: list[str] = []

    def handler(req):
        captured.append(str(req.url))
        return httpx.Response(200, json={"ok": True})

    for ch_type in REGISTRY:
        with mock_httpx_post(handler):
            result = dispatch(
                channel_type=ch_type,
                target_url=f"https://example.com/{ch_type}",
                signal=_signal("polaris.plan"),
            )
        assert result.status == "sent", f"{ch_type} expected sent, got {result.status}"

    assert sorted(captured) == [
        "https://example.com/discord",
        "https://example.com/mattermost",
        "https://example.com/slack",
        "https://example.com/teams",
    ]


def test_dispatch_formatter_exception_lands_failed_delivery():
    """A formatter that raises (e.g., a KeyError on a payload it
    didn't expect) lands status='failed' with formatter_exception
    rather than crashing the dispatcher."""
    sig = _signal("polaris.plan")

    # Patch slack.format to raise; dispatch must catch.
    def boom(_signal):
        raise RuntimeError("simulated formatter bug")

    with patch.object(notifications.slack, "format", boom):
        # Have to also bypass the cached REGISTRY tuple — REGISTRY
        # snapshotted slack.format at import time.
        with patch.dict(REGISTRY, {"slack": (boom, notifications.slack.post)}):
            result = dispatch(
                channel_type="slack",
                target_url="https://example.com/x",
                signal=sig,
            )
    assert result.status == "failed"
    assert result.response_summary["error"] == "formatter_exception"
    assert "simulated formatter bug" in result.response_summary["message"]
