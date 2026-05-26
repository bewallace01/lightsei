// Phase 29.2b: Apple universal-link verification file.
//
// iOS fetches this at app launch to learn which paths on
// app.lightsei.com should open in the Lightsei iOS app instead of
// Safari. Without this, every magic-link tap from Mail bounces to
// Safari and the installed PWA / native app never gets the token
// (the bug that closed Phase 28 in capture mode).
//
// Spec: https://developer.apple.com/documentation/xcode/supporting-associated-domains
//
// Components matched:
//   /c/auth/magic-link         exact match for the magic-link consume
//   /c/{slug}/conversation/*   future deep links from push notifications
//
// `appIDs` requires the Apple Developer Team ID. Until Bailey
// registers, LIGHTSEI_APPLE_TEAM_ID is unset and we emit an empty
// list. iOS rejects the AASA on real devices in that state, but
// the simulator with `applinks:app.lightsei.com?mode=developer`
// in the Associated Domains entitlement bypasses verification, so
// the universal-link routing can still be exercised end-to-end.

import { NextResponse } from "next/server";

// Force this route to be statically rendered so it's cacheable.
// AASA changes only when Team ID or path patterns change; both
// are infrequent.
export const dynamic = "force-static";

export async function GET() {
  const teamId = process.env.LIGHTSEI_APPLE_TEAM_ID || "";
  const bundleId = "com.lightsei.app";
  const appID = teamId ? `${teamId}.${bundleId}` : "";

  const body = {
    applinks: {
      details: [
        {
          appIDs: appID ? [appID] : [],
          components: [
            {
              "/": "/c/auth/magic-link",
              comment: "Phase 29.2: magic-link consume",
            },
            {
              "/": "/c/*/conversation/*",
              comment: "Phase 29.4: push notification deep links",
            },
          ],
        },
      ],
    },
  };

  return NextResponse.json(body, {
    headers: {
      // Apple's CDN respects Cache-Control here; one hour is a
      // reasonable balance between propagation and request volume.
      "Cache-Control": "public, max-age=3600",
    },
  });
}
