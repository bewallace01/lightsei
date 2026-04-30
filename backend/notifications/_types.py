"""Shared types for the notifications package.

A `Signal` is the pre-formatted payload the dispatcher hands off to a
per-platform formatter. Each formatter turns it into platform-native
JSON (Block Kit, Discord embed, Adaptive Card, Lightsei envelope).

A `Delivery` is the post-attempt audit record — what the dispatcher
writes to `notification_deliveries` after the HTTP-out completes.
Plain dicts on `response_summary` keep storage trivially JSON-
serializable.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class Signal:
    trigger: str                 # 'polaris.plan' | 'validation.fail' | 'run_failed' | 'test'
    agent_name: str
    dashboard_url: str           # deep link the formatter renders as a "View ↗" button
    timestamp: datetime
    payload: dict[str, Any]      # the source event's payload (or a synthetic dict for tests)
    workspace_id: str = ""       # only set for the generic-webhook envelope; native-chat formatters don't use it


@dataclass
class Delivery:
    status: str                          # 'sent' | 'failed'
    response_summary: dict[str, Any]
    attempt_count: int = 1
