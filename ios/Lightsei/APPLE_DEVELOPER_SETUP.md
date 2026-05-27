# Apple Developer setup for Lightsei iOS

The Lightsei iOS app (Phase 29) needs an Apple Developer Program
membership before three things actually work on a real device:

1. **Universal links** — taps on `https://app.lightsei.com/c/auth/magic-link?token=...` route into the installed app instead of Safari (today only the simulator with `?mode=developer` works)
2. **Sign in with Apple** — the OS auth sheet works without a Developer account, but our backend can't verify the identity token without a Team ID + bundle ID matching the App ID Apple has on file
3. **APNS push notifications** — generating the .p8 auth key + signing tokens that Apple's push service will accept

The good news: **everything else** (magic-link auth, vendor list, chat, paste-link fallback) already works in the simulator without the account.

This doc walks you through:
- Registering for the Developer Program ($99/yr)
- Configuring the App ID for `com.lightsei.app`
- Generating the APNS auth key
- Finding the Team ID
- Handing the artifacts to Claude to wire up

Expected total time: **30 min of clicking + 1-2 days of Apple's review** before the account is approved.

---

## Step 1: Register for the Apple Developer Program

Go to https://developer.apple.com/programs/enroll/

You'll need:
- An **Apple ID** with two-factor authentication enabled (your normal personal Apple ID is fine; Lightsei doesn't need a dedicated one)
- A **credit card** ($99 USD/yr, auto-renews unless you cancel before the renewal date)
- **Legal name + address** matching your government ID

Pick **Individual / Sole Proprietor** membership type. Reasons:
- You're a solo dev right now, no LLC formed
- Individual membership lists *your name* as the App Store seller — fine for v1; you can transfer to an Organization membership later (Apple supports this, no app re-submission needed) once Lightsei becomes a company
- Organization membership requires a D-U-N-S number (free but takes 1–2 weeks to issue, and Apple validates the business registry — overkill for now)

After payment, Apple reviews the enrollment. This typically takes **24-48 hours** but can stretch to a week. You'll get an email when approved.

**While waiting**, Claude can keep polishing the iOS app since the simulator works without an account.

---

## Step 2: Find your Team ID

Once you're approved, log into https://developer.apple.com/account

Top-right corner shows your membership info. The **Team ID** is a 10-character alphanumeric string (e.g. `ABCD12EFGH`). Copy it.

This goes into three places (Claude wires the env vars):

```
LIGHTSEI_APPLE_TEAM_ID = <your team id>      # for AASA appIDs (Phase 29.2b)
LIGHTSEI_APNS_TEAM_ID = <your team id>       # for APNS JWT signing (Phase 29.4)
```

It also gets set in Xcode's signing config so the iOS bundle can be code-signed.

---

## Step 3: Configure the App ID

In the Developer portal:

1. **Certificates, Identifiers & Profiles** → **Identifiers** → blue **+** button
2. Pick **App IDs**, then **App** (not App Clip)
3. **Description**: `Lightsei` (anything descriptive)
4. **Bundle ID** → **Explicit** → enter `com.lightsei.app` (must match `PRODUCT_BUNDLE_IDENTIFIER` in `ios/Lightsei/project.yml`)
5. **Capabilities** — tick these three:
   - ✓ **Associated Domains** (universal links)
   - ✓ **Push Notifications** (APNS)
   - ✓ **Sign In with Apple** (SIWA)
6. Click **Continue** → **Register**

If you already created an App ID with a different bundle ID by mistake, delete it + start over. Apple lets you reuse a bundle ID after deletion.

---

## Step 4: Generate the APNS Auth Key (.p8)

In the Developer portal:

1. **Certificates, Identifiers & Profiles** → **Keys** → blue **+** button
2. **Key Name**: `Lightsei APNS`
3. ✓ **Apple Push Notifications service (APNs)**
4. **Configure** next to APNs → confirm Team Selection → **Save**
5. **Continue** → **Register**
6. **DOWNLOAD** the `.p8` file. **This is your only chance** — Apple won't let you redownload it. If you lose it, you have to revoke + create a new key.
7. On the same page, copy the **Key ID** (10-char string).

You now have:
- `AuthKey_<KEY_ID>.p8` — the private key file
- `<KEY_ID>` — the key id string
- `<TEAM_ID>` — from step 2

These map to:

```
LIGHTSEI_APNS_KEY_ID = <KEY_ID>
LIGHTSEI_APNS_PRIVATE_KEY = <contents of AuthKey_*.p8 with newlines as \n>
LIGHTSEI_APNS_TEAM_ID = <TEAM_ID>
```

**Store the .p8 securely.** Treat like an API key — anyone with it can send push notifications to your users. Don't commit it to git. A password manager + a copy in 1Password or similar is fine.

---

## Step 5: Configure Sign in with Apple

Sign in with Apple needs a **Service ID** + a **Sign in with Apple Key** if you want web-based SIWA. For native iOS-only SIWA (what we're shipping in Phase 29.2c), the App ID's SIWA capability is enough — no extra setup.

If you ever add web SIWA on `app.lightsei.com/c`:

1. **Identifiers** → **+** → **Services IDs** → `com.lightsei.app.web` (different from bundle)
2. ✓ **Sign In with Apple** → **Configure** → tick the App ID from step 3 as primary
3. Add domain `app.lightsei.com` + return URL `https://app.lightsei.com/auth/end-user/sign-in-with-apple/callback` (Claude will wire that endpoint when needed)

For now, **skip this** — iOS-only SIWA works without it.

---

## Step 6: Hand the artifacts to Claude

Once you have:
- Team ID
- Key ID
- .p8 file contents

Paste them into the conversation (Claude can mask private values in the chat). Claude will:

1. Set the three env vars on Railway via `railway variable set`
2. Implement `apple_signin.verify_identity_token` (JWKS fetch + JWT validation)
3. Implement `apns.send_to_end_user` live path (JWT signing + HTTP/2 POST)
4. Add the Push Notifications + Sign in with Apple capability entries to `ios/Lightsei/project.yml` (currently commented out — they fail codesign without the Team ID)
5. Set the `DEVELOPMENT_TEAM` build setting in `project.yml` to your Team ID so Xcode can sign for real devices

That unlocks Phase 29.2c (real SIWA) + 29.4 (real iPhone push receipt). Phase 29.5 (TestFlight distribution) can start any time after.

---

## What does NOT block on the Developer account

You can keep using the simulator + magic-link auth path without the account. Specifically these all work today:

- All of the existing chat surface
- Magic-link sign-in via email
- Paste-link sign-in fallback inside the PWA / app
- Universal links **in the simulator** (the `?mode=developer` query bypasses Apple's AASA verification for sim builds)
- The SIWA button renders + opens the OS sheet (returns 501 from the backend since the verifier isn't live yet)
- APNS registration call fires + records the device token in the backend (no actual push delivery)

---

## Common gotchas

- **Two-factor authentication** is mandatory on the Apple ID used for the Developer Program. If your Apple ID doesn't have it on, turn it on at https://appleid.apple.com first.
- **Apple holds Macs on the wrong macOS version back from registering** for certain capabilities. macOS 13+ is recommended for the current Xcode + APNS workflow.
- **The .p8 file download is one-time only.** If you lose it, revoke the key in the portal + generate a new one. There's no fee.
- **Sign in with Apple lets the user hide their email** (Apple proxies `@privaterelay.appleid.com` addresses to your real inbox). The backend treats these as normal end-user emails; nothing extra needed.
- **The Team ID changes** if you transfer the Developer Program to an Organization later. Update the env vars when you do.

---

## Simulator runtime install (one-time machine config)

Unrelated to the Developer Program, the iOS simulator needs a runtime that matches Xcode's SDK version. If `xcodebuild` errors out compiling the asset catalog with a "No simulator runtime" message:

```bash
xcodebuild -downloadPlatform iOS
```

Or via the Xcode UI: **Settings → Components → iOS 26.x** → install. Each runtime is ~5GB.

After that, the asset catalog (app icon + accent color) compiles correctly and the indigo theme shows up in the simulator.
