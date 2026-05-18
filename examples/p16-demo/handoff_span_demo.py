"""Phase 16 demo, Act 6: human-mediated handoff joins two zones in the trace.

The trust-zone boundary is one-way by default: coral-crm-bot can't
dispatch to coral-research-bot directly (Act 5 proves this). But real
work needs the two sides to coordinate.

The intended path is a human-in-the-loop: the operator reads the
CRM-side output, decides what's safe to forward, and types a sanitized
prompt into the research-bot side. lightsei.handoff_span links the two
runs in the trace view so the chain reassembles in the dashboard —
without any data actually crossing the framework boundary.

Run this script from your laptop after both bots have at least one
recent run:

    export LIGHTSEI_API_KEY=bk_...    # workspace api key from /account
    export LIGHTSEI_API_URL=https://api.lightsei.com
    python examples/p16-demo/handoff_span_demo.py

Expected output:

    [setup] initializing SDK as operator (no agent_name)...
    [lookup] most recent coral-crm-bot run:       run_abc123
    [lookup] most recent coral-research-bot run:  run_def456
    [emit] handoff_span(from=run_abc123, to=run_def456, sanitized_prompt=...)
    [result] OK — handoff event written.
    [view] open https://app.lightsei.com/runs/run_abc123 to see the
           handoff edge to run_def456 in the trace view.

The trace view shows the two runs as a single connected chain even
though the data path was: CRM bot → human eyes → research bot. The
framework respected the zone boundary; the trace tells the operator
the whole story anyway.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import httpx
import lightsei


CRM_BOT_NAME = "coral-crm-bot"
RESEARCH_BOT_NAME = "coral-research-bot"


def latest_run(api_url: str, api_key: str, agent_name: str) -> Optional[str]:
    """Pull the most recent run id for `agent_name`. Returns None if
    the bot hasn't run yet on this workspace."""
    r = httpx.get(
        f"{api_url}/runs",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"agent_name": agent_name, "limit": 1},
        timeout=5.0,
    )
    r.raise_for_status()
    runs = r.json()
    if not runs:
        return None
    # The /runs endpoint returns a list of {id, agent_name, ...}.
    return runs[0]["id"]


def main() -> int:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    api_url = os.environ.get("LIGHTSEI_API_URL", "https://api.lightsei.com")
    if not api_key:
        print("LIGHTSEI_API_KEY must be set. Get one from /account.")
        return 1

    print("[setup] initializing SDK as operator (no agent_name)...")
    # No agent_name on init — this script runs as an operator, not a bot.
    # handoff_span emits a generic event, not bot-scoped.
    lightsei.init(api_key=api_key, base_url=api_url)

    print(f"[lookup] most recent {CRM_BOT_NAME} run:")
    crm_run = latest_run(api_url, api_key, CRM_BOT_NAME)
    if not crm_run:
        print(f"         no runs found for {CRM_BOT_NAME}. Trigger one first.")
        return 1
    print(f"           {crm_run}")

    print(f"[lookup] most recent {RESEARCH_BOT_NAME} run:")
    research_run = latest_run(api_url, api_key, RESEARCH_BOT_NAME)
    if not research_run:
        print(f"         no runs found for {RESEARCH_BOT_NAME}. Trigger one first.")
        return 1
    print(f"           {research_run}")

    # The operator's sanitized translation. In production this would be
    # whatever the operator decided was safe to forward — typically the
    # public-data parts only.
    sanitized_prompt = (
        "Research recent Series B SaaS funding rounds in the US for "
        "companies with 50-200 employees. Output: company name, round "
        "size, lead investor, headcount, public news from the last 90 "
        "days. Do not look up specific contacts."
    )

    print(
        f"[emit] handoff_span(from={crm_run}, to={research_run}, "
        f"sanitized_prompt=<{len(sanitized_prompt)} chars>)"
    )
    lightsei.handoff_span(
        from_run=crm_run,
        to_run=research_run,
        sanitized_prompt=sanitized_prompt,
        notes=(
            "Demo handoff. The CRM-side output listed at-risk accounts; "
            "the operator extracted only the company-shape (Series B, "
            "headcount band) and asked the research bot for public-side "
            "info on similar companies."
        ),
    )
    lightsei.flush()

    print("[result] OK — handoff event written.")
    print(
        f"[view] open {api_url.replace('api.', 'app.', 1)}/runs/{crm_run} "
        f"to see the handoff edge to {research_run} in the trace view."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
