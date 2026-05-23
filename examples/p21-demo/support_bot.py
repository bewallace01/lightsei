"""Phase 21.10 demo: customer-facing support bot.

The bot a Halo CS team would deploy after dropping
`halo-product-readme.md` into team-from-readme. Wires the Phase
21 widget chat surface together with the bot side's
`@lightsei.on_chat("widget")` handler.

Lives in the `public` trust zone. Required capabilities (auto-
granted on the dashboard via /widget-settings when the operator
picks the bot, but listed here for the bot author):

    - widget:respond
    - widget:escalate

Run mode:

    python support_bot.py

Environment:

    LIGHTSEI_API_KEY              # workspace API key (required)
    LIGHTSEI_BASE_URL             # default: https://api.lightsei.com
    LIGHTSEI_AGENT_NAME           # default: 'halo-support'
    ANTHROPIC_API_KEY             # the bot's LLM key (required —
                                    Halo runs the LLM call themselves;
                                    Lightsei meters but doesn't call)

The bot's logic is deliberately small. The Phase 21.10 demo
focuses on the widget surface, not on bot quality. Real bots
would carry a longer system prompt + an FAQ retrieval step;
this one demonstrates the wedge end-to-end.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Any

import lightsei


SYSTEM_PROMPT = """\
You are the Halo customer support bot. Halo is a build-monitoring
SaaS product for engineering teams.

You can confidently answer questions about:

- Pricing + plan tiers (Free up to 5 repos, Pro from $400/month,
  per-repo not per-seat).
- The basic integration-setup flow for GitHub Actions, CircleCI,
  and Buildkite (paste the build webhook URL into the CI
  provider's settings; paste the Halo API token as a secret).
- High-level product questions ("what does Halo do?", "do you
  support GitLab?", "what alerts can I get?").

You CANNOT answer:

- Account-specific questions ("my builds aren't showing up",
  "I can't see repo X", "my Slack alerts stopped").
- Billing details for a specific customer ("when does my plan
  renew?", "why was I charged $X?").
- Anything that requires looking up the user's account.

When a question falls into the second bucket, escalate to a
human via lightsei.LightseiEscalate. Don't apologize at length;
say one short sentence and escalate. The operator inbox will
pick it up.

Tone: short, friendly, factual. No exclamation marks.
"""


# Heuristics for "this question needs a human." A real bot would
# do this with an LLM call; the heuristic version makes the demo
# pipeline deterministic + cheap.
_ACCOUNT_SPECIFIC_KEYWORDS = (
    "my build", "my dashboard", "my account", "my repo", "my repos",
    "my alert", "my plan", "my invoice", "my billing", "i can't see",
    "i don't see", "not showing", "stopped working", "broken for me",
    "for me", "for our team", "for our org",
)


def _needs_escalation(user_message: str) -> bool:
    """Cheap heuristic: account-specific phrasing → escalate."""
    if not user_message:
        return False
    lower = user_message.lower()
    return any(kw in lower for kw in _ACCOUNT_SPECIFIC_KEYWORDS)


def _faq_lookup(user_message: str) -> str | None:
    """Tiny hardcoded FAQ. Stand-in for what a real bot would do
    via Anthropic + a retrieval step. Deterministic so the demo
    flow is stable + cheap."""
    if not user_message:
        return None
    lower = user_message.lower()

    if "pricing" in lower or "plan" in lower or "cost" in lower or "price" in lower:
        return (
            "Halo is free for up to 5 repos. Pro plans start at "
            "$400/month and are priced per repo, not per seat. The "
            "Pro plan includes flaky-test detection, cost-regression "
            "alerts, and Slack + PagerDuty integrations. See "
            "https://halo.dev/pricing for the full breakdown."
        )

    if "github actions" in lower or "github action" in lower:
        return (
            "To connect Halo to GitHub Actions:\n"
            "1. In Halo, go to Integrations → GitHub Actions and copy "
            "the build webhook URL.\n"
            "2. In your repo's GitHub settings, paste the URL under "
            "Webhooks. Set content-type to application/json.\n"
            "3. In Halo's same Integrations page, copy the API token "
            "and paste it as a repo secret named HALO_TOKEN.\n"
            "Your next push will show up in the Halo dashboard within "
            "about 30 seconds."
        )

    if "circleci" in lower or "circle ci" in lower:
        return (
            "For CircleCI: in Halo go to Integrations → CircleCI, "
            "copy the build webhook URL, and paste it into your "
            "project's notifications settings in CircleCI. Then "
            "paste the Halo API token as a CircleCI env var named "
            "HALO_TOKEN. The dashboard picks up your next build."
        )

    if "buildkite" in lower:
        return (
            "For Buildkite: in Halo go to Integrations → Buildkite, "
            "copy the webhook URL, and paste it into your pipeline's "
            "Notification Services. Then paste the Halo API token as "
            "a pipeline env var named HALO_TOKEN."
        )

    if "what does halo do" in lower or "what is halo" in lower:
        return (
            "Halo watches your CI for flaky tests, slow stages, and "
            "cost regressions. We surface them in a per-team and "
            "per-repo dashboard and alert via Slack or PagerDuty. "
            "Connect a repo in under a minute; you'll see your first "
            "build land within 30 seconds of your next push."
        )

    return None


@lightsei.on_chat("widget")
def handle_widget_turn(turn: dict[str, Any]) -> str | None:
    """Phase 21.10: one turn of a widget conversation.

    The 21.6 widget orchestrator + 21.5 bridge handler land here
    when the end user sends a message. Return a string to reply,
    return None / empty string to stay quiet, or raise
    LightseiEscalate to hand off to a human operator.
    """
    user_message = turn.get("user_message") or ""

    if _needs_escalation(user_message):
        # Hand off to the operator inbox. The bridge handler turns
        # this into a POST /widget-bot/escalate call + drops a
        # system message in the conversation.
        raise lightsei.LightseiEscalate(
            "account_specific_request",
            payload={
                "user_message": user_message[:300],
                "hint": "user asked about their specific account state.",
            },
        )

    faq = _faq_lookup(user_message)
    if faq:
        return faq

    # Anything else: short escalate. Don't try to BS through unknown
    # questions — escalating builds trust + gives Polaris cluster
    # data for the 21.9 incident-response loop.
    raise lightsei.LightseiEscalate(
        "unknown_question",
        payload={"user_message": user_message[:300]},
    )


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get(
        "LIGHTSEI_BASE_URL", "https://api.lightsei.com",
    )
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "halo-support")

    if not api_key:
        print("LIGHTSEI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    lightsei.init(
        api_key=api_key,
        agent_name=agent_name,
        version="0.1.0",
        base_url=base_url,
    )

    print(
        f"support bot up: agent={agent_name} base={base_url}",
        flush=True,
    )
    print("awaiting widget.chat commands.", flush=True)

    while True:
        try:
            lightsei.emit("support_bot_idle_tick", {})
        except Exception:
            traceback.print_exc()
        lightsei.flush(timeout=2.0)
        time.sleep(30)


if __name__ == "__main__":
    main()
