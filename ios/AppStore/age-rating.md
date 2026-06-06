# Lightsei — Age Rating Questionnaire (Phase 31.5.d)

Recommended answers for App Store Connect's Age Rating questionnaire. Apple
revised this questionnaire in 2025 (ratings are now 4+, 9+, 13+, 16+, 18+),
and the exact field wording can shift, so match these answers to the
questions as the live form presents them rather than to exact labels.

Lightsei iOS is a business / productivity chat client for talking to a team
of AI work assistants. It has no game content, no media library, no social
feed.

---

## Content categories — answer "None" for all of these

| Question | Answer |
|---|---|
| Cartoon or Fantasy Violence | None |
| Realistic Violence | None |
| Prolonged Graphic or Sadistic Realistic Violence | None |
| Profanity or Crude Humor | None |
| Mature / Suggestive Themes | None |
| Horror / Fear Themes | None |
| Medical / Treatment Information | None |
| Alcohol, Tobacco, or Drug Use or References | None |
| Sexual Content or Nudity | None |
| Graphic Sexual Content and Nudity | None |
| Simulated Gambling | None |

## Capability / other toggles

| Question | Answer | Why |
|---|---|---|
| Unrestricted Web Access | **No** | No in-app browser to arbitrary URLs. Chat is with specific configured bots only. |
| Gambling (real) | No | n/a |
| Contests | No | n/a |
| Made for Kids | No | B2B product; not directed at children. |

**Expected resulting rating: 4+.**

---

## Two judgment calls worth a conscious decision

Neither forces a higher rating, but both touch areas Apple has been
tightening, so decide on purpose rather than clicking through.

### 1. AI-generated content

The bots produce text via an LLM, so output is not 100% predictable. Apple
does NOT mandate 17+/18+ for AI, and Lightsei's bots are constrained
business assistants (not open-ended companion / roleplay AI), so 4+ is
defensible. If the questionnaire includes an AI-chatbot capability question,
answer it truthfully (yes, the app uses AI to generate responses); it does
not by itself raise the floor for a task-focused assistant.

- Recommended: answer truthfully, accept 4+.
- Alternative: if you want a conservative buffer against unpredictable
  output, you can self-select a higher minimum, but it is not required.

### 2. User communication / user-generated content

There is no public, user-to-user social feed. End users chat with a bot; on
the operator side, the inbox displays messages authored by end users. If the
questionnaire asks whether the app includes messaging or displays
user-generated content:

- The honest answer is that it includes private chat with bots and an
  operator inbox of end-user messages, not open user-to-user social posting.
- Apps with open UGC/communication can be pushed toward 13+ when there is no
  moderation. Lightsei's surface is private and business-scoped, so a low
  rating is defensible, but if a specific question maps cleanly to "users can
  communicate" / "displays UGC," answer it accurately and let the form
  compute the result.

---

## Note

I cannot see Apple's live questionnaire from here, so the field names above
are the stable/known ones. Enter the answers against whatever the current
form actually asks; the substance (no objectionable content, no open web
access, no real gambling, not made for kids) is what matters and is accurate
to the app.
