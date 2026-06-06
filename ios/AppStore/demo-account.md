# Lightsei — Demo Account for App Review (Phase 31.5.e)

What to put in App Store Connect > [version] > App Review Information, and
how to prepare the account so a reviewer can actually use the app.

---

## Why an OPERATOR account (not an end-user account)

The iOS app has two sign-in identities (see `SignInView.swift`):

- **Customer (end user):** Sign in with Apple, or magic-link by email.
  No password path. A reviewer cannot process a magic-link email, and
  Sign in with Apple would mint a fresh, empty end-user with no data.
  Not reviewer-friendly.
- **Business (operator):** magic-link by email, PLUS a "Use password
  instead" disclosure that calls `/auth/login` with email + password
  (commit 10d445a added this specifically for App Review).

So the demo account must be an **operator account with a password**, and the
reviewer signs in through the Business tab.

---

## Reviewer steps (include these verbatim in the Notes box)

```
Lightsei is the chat client for a hosted AI-coworker platform. Sign-in is required.

To sign in:
1. Launch the app. On the sign-in screen, tap the "Business" tab at the top.
2. Tap "Use password instead" near the bottom.
3. Enter the demo email and password provided in the fields above.
4. Tap Sign in. The app opens into the operator workspace.

What you can do: browse the team's channels and agents, open a conversation, and
send a message to see a reply. The Runs view shows recent agent activity.

No special hardware is required. The backend is hosted at api.lightsei.com.
```

Put the actual email in the **User name** field and the password in the
**Password** field (set "Sign-in required" = Yes). Do NOT put the password in
the Notes box.

---

## Creating the demo account (do this before submitting)

1. Go to https://app.lightsei.com and sign up with a dedicated demo email
   (e.g. a +alias you control, like `you+appreview@gmail.com`) and a
   password. This hits `POST /auth/signup`, which creates an operator User
   with a `password_hash` and a fresh workspace. That password is what the
   Business-tab "Use password instead" path authenticates against.

2. **Populate the workspace so the app is not empty.** Apple rejects apps
   that look non-functional (Guideline 2.1). At minimum:
   - Deploy or generate at least one bot (the team-from-README flow is the
     fastest way to get a believable team in one shot).
   - Open a conversation and exchange a couple of messages so the chat
     surface and the Runs view both show real content to the reviewer.

3. Sign in once on a real device or the simulator using the Business tab +
   "Use password instead" with these exact credentials, to confirm the path
   works before handing it to Apple.

4. Keep the demo account active and its workspace populated until review is
   complete. Do not delete its bots mid-review.

---

## Checklist

- [ ] Demo operator account created via web signup (email + password)
- [ ] Workspace has >= 1 bot and at least one real conversation
- [ ] Password sign-in verified on device/simulator (Business > Use password instead)
- [ ] App Store Connect: Sign-in required = Yes, User name + Password filled
- [ ] Notes box contains the reviewer steps above
