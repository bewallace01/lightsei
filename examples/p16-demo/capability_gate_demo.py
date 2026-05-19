"""Phase 16 demo, Act 4: SDK refuses an internet call from a PII-zone bot.

After deploying the Coral team via team-from-README with the Compliance
preset, the CRM-side bot (vega) lives in the 'pii' zone with
NO capabilities — including no 'internet' capability. The SDK's httpx
patch (Phase 16.3) refuses outbound network calls before they leave
the process.

Run this script from your laptop after the team is deployed:

    export LIGHTSEI_API_KEY=bk_...    # workspace api key from /account
    export LIGHTSEI_API_URL=https://api.lightsei.com
    python examples/p16-demo/capability_gate_demo.py

Expected output:

    [setup] initializing SDK as vega...
    [setup] capability list pulled from prod: []
    [attempt] httpx.get('https://api.linkedin.com/v2/...')
    [result] BLOCKED — LightseiCapabilityError raised before the network call
    [details] capability 'internet' not granted to agent 'vega'
              (granted: none — default-deny).

This is the wedge in action: prompt-injected CRM bots can't exfiltrate
data because the framework refuses the call. The dashboard's /agents
editor can grant 'internet' if a human operator explicitly decides to
open that path; the default is closed.
"""
from __future__ import annotations

import os
import sys

import httpx
import lightsei
from lightsei.errors import LightseiCapabilityError


CRM_BOT_NAME = "vega"  # PII-zone bot from the deployed Coral team
                       # (does HubSpot enrichment; tagged 'pii' by the
                       # planner; zero capabilities under Compliance).


def main() -> int:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    api_url = os.environ.get("LIGHTSEI_API_URL", "https://api.lightsei.com")
    if not api_key:
        print("LIGHTSEI_API_KEY must be set. Get one from /account.")
        return 1

    print(f"[setup] initializing SDK as {CRM_BOT_NAME}...")
    lightsei.init(
        api_key=api_key,
        agent_name=CRM_BOT_NAME,
        base_url=api_url,
    )

    # Surface the capability list so the demo viewer can see the default
    # is empty under the Compliance preset. The list is cached on the
    # internal `_client` after init() fetches it from the backend.
    from lightsei import _client as _sdk_client
    granted = getattr(_sdk_client, "_capabilities_cache", []) or []
    print(f"[setup] capability list pulled from prod: {granted}")

    print("[attempt] httpx.get('https://api.linkedin.com/v2/people/me')")
    try:
        httpx.get("https://api.linkedin.com/v2/people/me", timeout=2.0)
    except LightseiCapabilityError as exc:
        print("[result] BLOCKED — LightseiCapabilityError raised before the network call")
        print(f"[details] {exc}")
        return 0
    except httpx.HTTPError as exc:
        # If we got here, the framework let the call through (or the
        # network is unreachable). Either way the gate didn't fire.
        print(f"[result] UNEXPECTED — httpx error reached: {exc}")
        print("         The gate did not engage. Check the agent's capability")
        print("         list in the dashboard; it should be empty under")
        print("         the Compliance preset's specialist role.")
        return 1

    print("[result] UNEXPECTED — call went through with no error.")
    print("         The gate did not fire. Check the agent's capability list.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
