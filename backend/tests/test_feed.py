"""Phase 34.2: proactive-feed tests.

Pure build_feed_item mapping + the endpoint round-trip.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import feed
from db import session_scope
from models import Event, Workspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ev(kind, agent, payload, *, ts=None, id=1):
    return {"id": id, "kind": kind, "agent_name": agent,
            "payload": payload, "timestamp": ts or _now()}


# ---------- pure mapping ---------- #


def test_negative_review_is_an_alert():
    item = feed.build_feed_item(_ev(
        "reputation.analyzed", "reputation",
        {"sentiment": "negative", "author": "Dana", "rating": 1,
         "severity": "error"},
    ))
    assert item["severity"] == "alert"
    assert "negative review from Dana" in item["title"]
    # Constellation identity: star name + role.
    assert item["assistant_name"] == "Lyra"
    assert item["assistant_role"] == "Reputation"
    assert item["assistant_label"] == "Lyra · Reputation"


def test_positive_review_is_info():
    item = feed.build_feed_item(_ev(
        "reputation.analyzed", "reputation",
        {"sentiment": "positive", "author": "Sam", "rating": 5,
         "severity": "info"},
    ))
    assert item["severity"] == "info"


def test_lead_score_renders_score_and_quality():
    item = feed.build_feed_item(_ev(
        "lead.scored", "lead",
        {"score": 88, "quality": "hot", "lead": {"name": "Acme"},
         "severity": "error"},
    ))
    assert "88" in item["title"] and "hot" in item["title"]
    assert item["severity"] == "alert"


def test_urgent_email_flagged():
    item = feed.build_feed_item(_ev(
        "inbox.processed", "inbox",
        {"category": "billing", "urgency": "high", "needs_human": True,
         "subject": "Double charged", "severity": "error"},
    ))
    assert "needs you" in item["title"]
    assert item["severity"] == "alert"


def test_website_down_vs_ok():
    down = feed.build_feed_item(_ev(
        "website.check_complete", "website",
        {"up": False, "url": "https://x.com", "severity": "error"},
    ))
    assert "down" in down["title"] and down["severity"] == "alert"
    ok = feed.build_feed_item(_ev(
        "website.check_complete", "website",
        {"up": True, "broken_links": [], "url": "https://x.com",
         "severity": "info"},
    ))
    assert "all good" in ok["title"] and ok["severity"] == "info"


def test_bi_summary_vs_answer():
    s = feed.build_feed_item(_ev("bi.summary", "bi",
                                 {"kind": "summary", "summary": "Good week."}))
    assert s["title"] == "Business summary ready"
    a = feed.build_feed_item(_ev("bi.summary", "bi",
                                 {"kind": "answer", "summary": "12 leads."}))
    assert a["title"] == "Answered a question"


def test_crash_is_alert():
    item = feed.build_feed_item(_ev("bi.crash", "bi",
                                    {"error": "no ANTHROPIC_API_KEY"}))
    assert item["severity"] == "alert"
    # Crash title uses the assistant's star name.
    assert item["title"] == "Altair hit an error"


def test_name_override_wins():
    item = feed.build_feed_item(
        _ev("bi.summary", "bi", {"kind": "summary", "summary": "x"}),
        name_overrides={"bi": "Numbers"},
    )
    assert item["assistant_name"] == "Numbers"
    assert item["assistant_label"] == "Numbers · Business Intelligence"


def test_non_feed_event_dropped():
    assert feed.build_feed_item(_ev("connector_call_completed", "bi", {})) is None


def test_detail_is_truncated():
    item = feed.build_feed_item(_ev("marketing.created", "marketing",
                                    {"task": "ad_copy", "content": "x" * 500}))
    assert len(item["detail"]) <= 141
    assert "Marketing ad copy draft ready" == item["title"]


# ---------- endpoint ---------- #


def test_feed_endpoint_orders_newest_first_and_filters(client, alice):
    h = auth_headers(alice["session_token"])
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        # Two feed-worthy events + one that must be filtered out.
        s.add(Event(workspace_id=ws_id, run_id=str(uuid.uuid4()),
                    agent_name="lead", kind="lead.scored",
                    payload={"score": 70, "quality": "warm", "severity": "info"},
                    timestamp=_now() - timedelta(hours=2)))
        s.add(Event(workspace_id=ws_id, run_id=str(uuid.uuid4()),
                    agent_name="reputation", kind="reputation.analyzed",
                    payload={"sentiment": "negative", "author": "Dana",
                             "severity": "error"},
                    timestamp=_now() - timedelta(minutes=5)))
        s.add(Event(workspace_id=ws_id, run_id=str(uuid.uuid4()),
                    agent_name="bi", kind="connector_call_completed",
                    payload={}, timestamp=_now()))

    body = client.get("/workspaces/me/feed", headers=h).json()
    items = body["items"]
    assert len(items) == 2  # the connector event is filtered
    # Newest first: the reputation alert precedes the older lead score.
    assert items[0]["kind"] == "reputation.analyzed"
    assert items[1]["kind"] == "lead.scored"


def test_feed_empty_for_fresh_workspace(client, alice):
    h = auth_headers(alice["session_token"])
    assert client.get("/workspaces/me/feed", headers=h).json()["items"] == []


def test_feed_item_seo_audit():
    from feed import build_feed_item
    item = build_feed_item({
        "kind": "seo.audit_complete", "agent_name": "seo",
        "payload": {"url": "https://acme.com", "score": 70, "issues": 2, "reachable": True},
    })
    assert "70/100" in item["title"]
    assert "2 issue" in item["detail"]


def test_feed_item_seo_page_drafted():
    from feed import build_feed_item
    item = build_feed_item({
        "kind": "seo.page_drafted", "agent_name": "seo",
        "payload": {"keyword": "plumber austin",
                    "page": {"h1": "Plumber in Austin", "meta_description": "Fast help"}},
    })
    assert "Plumber in Austin" in item["title"]
