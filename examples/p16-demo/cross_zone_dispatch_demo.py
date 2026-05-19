"""Phase 16 demo, Act 5: SDK refuses a forbidden dispatch.

After the Coral team is deployed with the Compliance preset, the
PII-side bot (vega) tries to lightsei.send_command(...) to the
public-side bot (rigel). The SDK refuses BEFORE the network call,
proving prompt injection / runaway loops can't move PII data across
the zone boundary.

Under Compliance, two backstops apply, in order:

1. Capability gate (Phase 16.3): vega has NO capabilities — not even
   'send_command' — so the SDK refuses on the capability check before
   it ever evaluates the target's zone. This is the gate that fires
   in the standard Compliance demo.

2. Cross-zone gate (Phase 16.4): even if vega WERE granted
   'send_command' (someone overrode the preset), dispatching from
   pii → public would still be refused because
   dispatches_cross_zone=False on vega.

The script catches BOTH error types so the demo works regardless of
which gate fires. Strong guarantee: two backstops, the more
restrictive one fires first.

Run from your laptop after the team is deployed:

    export LIGHTSEI_API_KEY=bk_...    # workspace api key from /account
    export LIGHTSEI_API_URL=https://api.lightsei.com
    python examples/p16-demo/cross_zone_dispatch_demo.py

Expected output (default Compliance state):

    [setup] initializing SDK as vega (zone='pii')...
    [attempt] vega → send_command(rigel, kind='research_company')
    [result] BLOCKED — LightseiCapabilityError raised before the network call
    [details] capability 'send_command' not granted to agent 'vega'
              (granted: none — default-deny).
              A pii bot literally cannot initiate send_command — the
              capability gate refuses even before the cross-zone gate
              would. Two backstops; the more restrictive one fires first.

The wedge claim — your CRM data cannot leak to an internet-side bot
via prompt injection, runaway agent loops, or accidental developer
wiring. The framework enforces the boundary, not vibes. The only
sanctioned way data crosses zones is a human-mediated
lightsei.handoff_span (Act 6).
"""
from __future__ import annotations

import os
import sys

import lightsei
from lightsei.errors import LightseiCapabilityError, LightseiCrossZoneError


CRM_BOT_NAME = "vega"
RESEARCH_BOT_NAME = "rigel"


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
            target_agent=RESEARCH_BOT_NAME,
            kind="research_company",
            payload={
                # In a real exfiltration scenario this is where customer
                # PII would land. We use obviously fake values so nothing
                # real ever rides this code path.
                "company_name": "Acme Inc",
                "contact_email": "fake@example.com",
            },
        )
    except LightseiCapabilityError as exc:
        # Under the Compliance preset, vega has NO capabilities — not
        # even 'send_command'. So the capability gate fires BEFORE
        # we even reach the cross-zone check. That's actually the
        # stronger guarantee: pii bots literally can't initiate a
        # dispatch at all, let alone one that targets a different zone.
        print("[result] BLOCKED — LightseiCapabilityError raised before the network call")
        print(f"[details] capability {exc.capability!r} not granted to agent {exc.agent_name!r}")
        print(f"          (granted: {exc.granted or 'none — default-deny'}).")
        print("          A pii bot literally cannot initiate send_command — the")
        print("          capability gate refuses even before the cross-zone gate")
        print("          would. Two backstops; the more restrictive one fires first.")
        return 0
    except LightseiCrossZoneError as exc:
        # Fallback for source bots that DO have send_command but try
        # to dispatch across a zone boundary (e.g. polaris → vega).
        # Under the Compliance preset's hint-aware mapping, pii bots
        # don't have send_command, so this branch usually doesn't fire
        # with vega as the source — but it would for polaris → vega
        # if you wanted to exercise the cross-zone gate specifically.
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
