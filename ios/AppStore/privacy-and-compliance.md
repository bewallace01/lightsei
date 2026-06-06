# Lightsei — App Privacy + Export Compliance (Phase 31.5.c + 31.5.f)

Inputs for App Store Connect > App Privacy and the Export Compliance
questionnaire. Determined from the actual iOS + backend code, not assumed.

---

## 31.5.f — Export Compliance (the App Encryption Documentation dialog)

**Answer: "None of the algorithms mentioned above."**

Basis (verified in code):
- Network calls use Apple's `URLSession` over HTTPS/TLS (OS-provided).
- Session tokens stored via Apple's Keychain (`Security` framework).
- No proprietary crypto, no bundled standard-crypto library (no CryptoKit,
  CommonCrypto, OpenSSL, libsodium, SQLCipher).

The app only *uses* iOS's built-in encryption; it does not *implement* its
own. Result: the app is exempt, no encryption documentation required.

**Permanent fix (recommended):** add to `ios/Lightsei/Resources/Info.plist`
so this dialog stops appearing on every upload:

```xml
<key>ITSAppUsesNonExemptEncryption</key>
<false/>
```

`false` asserts "does not use non-exempt encryption" (i.e. exempt), which is
correct for an HTTPS-plus-Keychain app.

---

## 31.5.c — App Privacy nutrition labels

### Summary answers for the questionnaire

- Does this app collect data?  **Yes.**
- Is any data used to track you?  **No.** (No ad SDKs, no analytics SDK, no
  data brokers, no cross-app/site tracking. Confirmed: no third-party
  tracking or analytics frameworks in the iOS or web codebase.)
- All collected data is **linked to the user's identity** (the app is
  account-based: you sign in with an email).

### Data types to declare (all: Linked to You, NOT used for tracking)

| Data Type | Apple Category | Why collected (Purpose) | Source in code |
|---|---|---|---|
| Email Address | Contact Info | App Functionality (account, sign-in, magic-link) | `User.email`, `EndUser.email` |
| Name | Contact Info | App Functionality (display name shown in chat) | `EndUser.display_name` (optional) |
| Other User Content | User Content | App Functionality (chat messages to/from bots) + Customer Support | chat surface (SlackShellView) |
| Device ID | Identifiers | App Functionality (push notifications) | `end_user_apns_tokens.device_token` |

For every row above:
- "Is this data linked to the user's identity?" -> **Yes**
- "Is this data used to track you?" -> **No**
- Purpose -> **App Functionality** (add **Customer Support** as a second
  purpose on User Content, since a support-bot is a core use case)

### Data types to mark NOT collected

Health, Financial Info, Location, Sensitive Info, Contacts, Browsing
History, Search History, Purchases, Usage Data, Diagnostics, Audio,
Photos/Video, Gameplay, and all other categories.

Notes:
- **Diagnostics / Crash data: not collected.** No crash-reporting or
  analytics SDK is integrated. (If Apple-level crash sharing is ever added,
  that is user-opted-in at the OS layer and still not "collected by the app.")
- **Usage Data: not collected.** The platform's run/event telemetry is the
  operator's own agent observability data, not app-usage analytics gathered
  about the person using this app.

### DECIDED (2026-06-05): declare the APNS push token as Device ID

Operator chose to declare it. The table above is final. Background kept below.

### Background: the APNS push token (Device ID)

Apple's "Device ID" definition centers on cross-app device identifiers
(e.g. the advertising identifier). An APNS push token is used solely to
deliver notifications and is not used to track the device across apps, so
some teams do not declare it. The conservative, defensible choice is to
declare it under **Identifiers > Device ID, App Functionality, linked,
not tracking**, which the table above does.

Pick one:
- **Declare it (recommended):** strictly accurate, zero rejection risk on
  this point. Cost: a "Device ID" chip appears on your privacy card.
- **Omit it:** arguable under Apple's narrow Device-ID definition, cleaner
  card. Slightly higher (still low) chance a reviewer questions it.

If you omit Device ID, the remaining declared types are Email, Name, and
User Content, all Linked / App Functionality / not tracking.
