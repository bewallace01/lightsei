"""Phase 16 demo, Act 5: SDK refuses a cross-zone dispatch.

After the Coral team is deployed with the Compliance preset:

  - coral-crm-bot         sensitivity_level='pii',    dispatches_cross_zone=False
  - coral-research-bot    sensitivity_level='public', dispatches_cross_zone=False

When coral-crm-bot tries to `lightsei.send_command(agent='coral-research-bot', ...)`,
the SDK's command dispatcher (Phase 16.4) checks the target's zone
BEFORE making the network call, sees a mismatched zone with cross-zone
disabled, and raises LightseiCrossZoneError.

Run this script from your laptop after the team is deployed:

    export LIGHTSEI_API_KEY=bk_...    # workspace api key from /account
    export LIGHTSEI_API_URL=https://api.lightsei.com
    python examples/p16-demo/cross_zone_dispatch_demo.py

Expected output:

    [setup] initializing SDK as coral-crm-bot (zone='pii')...
    [attempt] coral-crm-bot → send_command(coral-research-bot, kind='research_company')
    [result] BLOCKED — LightseiCrossZoneError raised before the network call
    [details] source: coral-crm-bot (pii)
              target: coral-research-bot (public)
              dispatches_cross_zone is False on the source — the framework
              refuses the call. The only way data crosses the zone is via
              a human-mediated handoff with lightsei.handoff_span.

The wedge claim — your CRM data cannot leak to an internet-side bot via
prompt injection, runaway agent loops, or accidental developer wiring.
The framework enforces the boundary, not vibes.
"""
from __future__ import annotations

import os
import sys

import lightsei
from lightsei.errors import LightseiCrossZoneError


CRM_BOT_NAME = "coral-crm-bot"
RESEARCH_BOT_NAME = "coral-research-bot"


def main() -> int:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    api_url = os.environ.get("LIGHTSEI_API_URL", "https://api.lightsei.com")
    if not api_key:
        print("LIGHTSEI_API_KEY must be set. Get one from /account.")
        return 1

    print(f"[setup] initializing SDK as {CRM_BOT_NAME} (zone='pii')...")
    lightsei.init(
        api_key=api_key,
        agent_name=CRM_BOT_NAME,
        base_url=api_url,
    )

    print(
        f"[attempt] {CRM_BOT_NAME} → "
        f"send_command({RESEARCH_BOT_NAME}, kind='research_company')"
    )
    try:
        lightsei.send_command(
            agent=RESEARCH_BOT_NAME,
            kind="research_company",
            payload={
                # In a real exfiltration scenario this is where customer
                # PII would land. We use obviously fake values so nothing
                # real ever rides this code path.
                "company_name": "Acme Inc",
                "contact_email": "fake@example.com",
            },
        )
    except LightseiCrossZoneError as exc:
        print("[result] BLOCKED — LightseiCrossZoneError raised before the network call")
        print(f"[details] source: {exc.source_agent} ({exc.source_zone})")
        print(f"          target: {exc.target_agent} ({exc.target_zone})")
        print("          dispatches_cross_zone is False on the source — the")
        print("          framework refuses the call.")
        return 0
    except Exception as exc:
        print(f"[result] UNEXPECTED — different error reached: {type(exc).__name__}: {exc}")
        return 1

    print("[result] UNEXPECTED — send_command returned without raising.")
    print("         The gate did not fire. Check that:")
    print(f"           - {CRM_BOT_NAME} has sensitivity_level='pii'")
    print(f"           - {RESEARCH_BOT_NAME} has sensitivity_level='public'")
    print(f"           - {CRM_BOT_NAME} has dispatches_cross_zone=False")
    return 1


if __name__ == "__main__":
    sys.exit(main())
