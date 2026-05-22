"""Phase 20.10 demo, Act 5: backend refuses a wrong-zone connector call.

The digest bot lives in `internal` — Gmail / Calendar / Drive all
declare zones that include `internal`. Calls go through.

But imagine an operator (or a prompt injection convincing the
operator) tries to deploy a `public`-zoned bot that calls
`lightsei.gmail.list_labels()`. Gmail's declared_zones is
`{internal, sensitive, pii}` — public is excluded. The backend's
bot-callable endpoint (Phase 20.6) refuses the call with
`403 connector_zone_mismatch`, which the SDK surfaces as
`LightseiConnectorZoneError`.

This script proves the wedge end-to-end, without needing real
Google OAuth.

Setup:

    # 1. An API key for a workspace with the connector machinery.
    export LIGHTSEI_API_KEY=bk_...
    export LIGHTSEI_BASE_URL=https://api.lightsei.com

    # 2. A `public`-zoned bot with the connector:gmail capability
    #    granted. The capability alone is not enough — the zone
    #    check still has to refuse. Create it via the dashboard
    #    (/agents → New agent → public + connector:gmail) or via
    #    the API:
    #
    #        POST /agents
    #        { "name": "researcher", "role": "specialist",
    #          "sensitivity_level": "public",
    #          "capabilities": ["connector:gmail"], ... }

    # 3. A Gmail install on the workspace (so we get past the
    #    "no install" 400 and reach the zone gate). Doesn't matter
    #    which Google account.

    python examples/p20-demo/zone_enforcement_demo.py

Expected output:

    [setup] initializing SDK as researcher (sensitivity=public)
    [setup] cached capabilities: ['connector:gmail']
    [attempt] lightsei.gmail.list_labels()
    [result] BLOCKED — LightseiConnectorZoneError raised
    [details] connector 'gmail' refused calls from
              'public'-zoned bots. Declared zones:
              ['internal', 'pii', 'sensitive'].

This is the wedge: the capability check would let the call through
(because the operator did grant `connector:gmail`), but the zone
check refuses anyway. Customer-data connectors are out of reach for
public-zoned bots even when an operator misconfigures the
capability list.
"""
from __future__ import annotations

import os
import sys

import lightsei
from lightsei._client import _client
from lightsei.errors import (
    LightseiCapabilityError,
    LightseiConnectorZoneError,
    LightseiError,
)


BOT_NAME = os.environ.get("LIGHTSEI_AGENT_NAME", "researcher")


def main() -> int:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    if not api_key:
        print("LIGHTSEI_API_KEY not set", file=sys.stderr)
        return 1

    print(f"[setup] initializing SDK as {BOT_NAME}")
    lightsei.init(
        api_key=api_key, agent_name=BOT_NAME,
        version="0.0.1", base_url=base_url,
    )
    print(f"[setup] cached capabilities: "
          f"{getattr(_client, '_capabilities_cache', None)}")

    # Sanity check — if the local cache doesn't have connector:gmail,
    # the SDK would raise LightseiCapabilityError before hitting the
    # backend, and the demo wouldn't actually prove the zone gate.
    cap_cache = getattr(_client, "_capabilities_cache", None) or []
    if "connector:gmail" not in cap_cache:
        print(
            f"[skip] agent {BOT_NAME!r} doesn't have 'connector:gmail' "
            "granted. The capability check would refuse before the "
            "zone gate even runs. Grant connector:gmail on /agents/"
            f"{BOT_NAME}/capabilities and retry.",
            file=sys.stderr,
        )
        return 2

    print(f"[attempt] lightsei.gmail.list_labels()")
    try:
        lightsei.gmail.list_labels()
    except LightseiConnectorZoneError as exc:
        print(f"[result] BLOCKED — LightseiConnectorZoneError raised")
        print(f"[details] connector {exc.connector_type!r} refused "
              f"calls from {exc.agent_sensitivity_level!r}-zoned "
              f"bots. Declared zones: {sorted(exc.declared_zones)}.")
        return 0
    except LightseiCapabilityError as exc:
        # Capability cache was stale, server's truth was 'no'. The
        # demo still proves a gate fired, just not the one we
        # wanted. Surface what happened.
        print(f"[result] BLOCKED on capability — not the zone gate "
              f"(capability {exc.capability!r} not granted to "
              f"{exc.agent_name!r}).", file=sys.stderr)
        return 3
    except LightseiError as exc:
        # 400 connector_not_installed lands here. Surface what's
        # missing so the operator can fix it.
        print(f"[skip] backend returned {exc} — likely no Gmail "
              "install on this workspace. Install Gmail first from "
              "/integrations, then re-run.", file=sys.stderr)
        return 4

    # Should never reach here — the zone gate must refuse the call.
    print("[unexpected] the call succeeded; the zone gate did NOT "
          "fire. This is a regression in Phase 20.6. Investigate.",
          file=sys.stderr)
    return 5


if __name__ == "__main__":
    sys.exit(main())
