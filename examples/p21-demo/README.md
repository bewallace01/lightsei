# Phase 21 — customer-facing widget demo

End-to-end demo for Phase 21. An operator at "Halo" (the fake build-monitoring SaaS in `halo-product-readme.md`) wires up the customer-facing chat widget, pastes a snippet onto a fake product page, and watches the full pipeline run: end user asks → bot deflects or escalates → operator triages in `/inbox` → Polaris notices the pattern → operator applies the suggested fix → bot retries with new guidance.

Pairs with the Phase 16 wedge demo (`examples/p16-demo/`) and the Phase 20 connector demo (`examples/p20-demo/`). Together: 16 proves trust zones stop leaks, 20 proves bots can do real work via connectors, 21 proves the same machinery extends to surfaces where the customer's own end users interact with the bots.

## What you'll need

- A Lightsei workspace on prod (paid plan or free credits remaining). Sign up at https://app.lightsei.com if you don't have one.
- An Anthropic API key set as the workspace secret `ANTHROPIC_API_KEY` (used by team-from-readme generation, by `/widget-incident-response/scan`, and by the bot itself if you swap the heuristic for an LLM).
- Python 3.11+ on your laptop with the Lightsei SDK installed (`pip install -e ./sdk`).
- ~15 minutes for the demo, plus ~$0.50-$1.00 of Anthropic cost for the planner + bot deploys + Polaris pattern-detection call.

## Files in this directory

- `halo-product-readme.md` — what an operator drops into team-from-readme to generate the customer-facing bot.
- `support_bot.py` — the deployable bot. Uses `@lightsei.on_chat("widget")` + `LightseiEscalate`. ~200 lines.
- `embed-example.html` — fake Halo product page that pastes the widget snippet at the bottom.
- `README.md` — this file.

## Demo flow (5 acts)

### Act 1 — wire up the widget

Open `/widget-settings` in the dashboard.

The first time you load this page, Lightsei mints a `widget_public_id` for your workspace and persists it. Subsequent loads return the same id. Copy the snippet at the bottom of the page — you'll need it in Act 2.

Click the bot picker dropdown — it lists every deployed agent in your workspace. Pick the bot you want to answer customer questions. The PATCH auto-grants `widget:respond` + `widget:escalate` to the bot if missing (so you can't accidentally pick a bot that can't reply).

Add at least one origin to the allowed-origins textarea. For local testing this should be `http://localhost:PORT` (the port your `embed-example.html` ends up on — see Act 2). For prod, list every domain that hosts the widget (`https://halo.dev`, `https://www.halo.dev`, etc.). Click **Save origins**.

If you haven't deployed a customer-facing bot yet:

1. Open `/agents/team-from-readme`.
2. Paste `halo-product-readme.md` into the textarea.
3. Click **Plan a team**. The planner returns a bot named after a star (probably "vega" or "altair"), `public` zone, with a sensible system prompt for Halo's pricing + integration FAQs.
4. Click **Generate code**. Review. Click **Deploy**.
5. Go back to `/widget-settings`, pick this bot in the dropdown.

(Alternatively: deploy `support_bot.py` directly, name the agent whatever you want, set sensitivity to `public`, grant `widget:respond` + `widget:escalate` + `chat`.)

### Act 2 — paste the snippet

Open `examples/p21-demo/embed-example.html`. The `<script>` at the bottom points at `http://localhost:3000/widget.js` for local development; replace `localhost:3000` with `https://app.lightsei.com` for prod. Replace `data-workspace="preview-id"` with the `widget_public_id` from Act 1.

Open the file in a browser. The Halo product page renders + a chat bubble appears in the bottom-right. Click it — the iframe expands to show "About Vega · Online" (or whatever your bot is named).

Type a deflectable question: **"What's included in the Pro plan?"** Press Enter.

Within 2-3 seconds, the bot's reply lands in the iframe ("Halo is free for up to 5 repos. Pro plans start at $400/month..."). Open `/inbox` in another tab — a new conversation row appears with the question's preview, the `public` zone chip, and "Open" status.

### Act 3 — escalate + take over

Open the widget again (or click "Start over" in the header for a fresh thread). Ask an account-specific question: **"My builds aren't showing up in the dashboard."**

The bot's `_needs_escalation` heuristic flags this as account-specific. It raises `LightseiEscalate("account_specific_request", ...)`. The bridge handler in the SDK posts to `/widget-bot/escalate`. A system message lands in the iframe: "This conversation has been handed off to a human."

In `/inbox`, the conversation flips to the top of the list with an "Escalated" red badge and an "open escalation" count of 1. Click it. The right pane shows the thread + an "Open escalation: account_specific_request" panel with the captured payload.

Click **Take over** (amber button). The status flips to "Handling"; a system message lands in the thread ("An operator has joined the conversation."). The end-user iframe's subtitle updates to "A human has joined this conversation".

Type a reply in the inbox textarea: **"Looking into your builds now. What's the repo name?"** Press ⌘+Enter (or Ctrl+Enter on Linux/Windows).

The end user sees the reply land in the iframe with an emerald right-aligned bubble labeled `operator`. The bot stays paused — the next end-user message records but doesn't trigger an orchestrator job.

Click **Mark resolved** when you're done. Status flips to "Resolved"; the operator inbox no longer surfaces the row by default (switch the filter to "Resolved" to see it).

### Act 4 — generate the pattern

Open the widget on `embed-example.html` again (or in a new tab — the localStorage anon id will be fresh). Ask three more account-specific questions:

1. **"My dashboard says 'no data' for my main repo."**
2. **"I can't see my Slack alerts anymore."**
3. **"My builds aren't showing up since yesterday."**

Each one escalates. Open `/inbox`; all three appear as "Escalated" rows.

Now click **Scan for patterns** in the inbox header (top right).

The backend's `widget_incident_response.find_escalation_clusters` runs:

1. Groups by `reason` keyword (all four — Act 3's + Act 4's — share `account_specific_request`).
2. Inside that group, runs token-overlap clustering on the user messages (keywords like `builds`, `dashboard`, `showing`, `alerts` cluster these together).
3. Picks the freshest 5 user messages per cluster as samples, truncated.

If the cluster is ≥ 3 escalations, the scan calls Anthropic to draft a `suggested_fix` (`{kind, summary, detail}`). The fix persists on every escalation row in the cluster + `polaris.issue_pattern` event fires on a `lightsei.system` run. A flash bar reports "1 suggested fix ready."

Open any of the four escalated conversations in `/inbox`. The open-escalation panel now grows a **"Polaris suggested:"** card with the proposed system-prompt addendum + Apply / Dismiss buttons.

### Act 5 — apply the fix

Click **Apply suggested fix** on the most recent escalation.

The backend:

1. Mutates the bot's `system_prompt` — appends `# Polaris-suggested fix applied <iso>\n<detail>` at the end so an operator can find and revert it later.
2. Marks the escalation resolved with `resolved_by_user_id` = your user id.
3. Drops a system message in the conversation: "Polaris updated the bot based on this conversation and similar ones. The bot will try again with new guidance."
4. Flips the conversation status back to `open` so the bot resumes for any new user messages.

Open the widget again. Ask **"My builds are not showing up"** (similar phrasing to the originals). The bot now has the new guidance in its system prompt. Depending on what Anthropic drafted, you'll typically see one of two outcomes:

- The bot answers cleanly ("Sorry to hear that — start by checking the Halo dashboard's Connections page..."), no escalation. Deflection win.
- The bot still escalates but with a smarter routing reason / payload (e.g. `incomplete_setup` instead of generic `account_specific_request`).

Either outcome demonstrates the self-improvement loop in motion. In a real deployment, the operator + Polaris keep iterating until similar questions deflect cleanly.

## What this demo proves

1. **The wedge extends to customer-facing surfaces.** The Halo bot lives in the `public` zone. If an operator misconfigured it to try `lightsei.gmail.send_email(...)`, the backend (Phase 20.6) would refuse with `connector_zone_mismatch`. The trust-zone gate doesn't care whether the bot is internal or customer-facing — same enforcement, same surface.
2. **Operator-in-the-loop is legible.** Every escalation surfaces in `/inbox` with the trigger reason + the captured payload + the conversation history. Take-over pauses the bot cleanly; reply lands as `operator`-role in the thread (the end user sees "(human reply)" framing in the iframe).
3. **Self-improvement is observable.** Polaris's scan + the suggested-fix machinery turn repeated escalations into operator-applyable bot improvements. The diff is a marked section in the bot's `system_prompt`, easy to review + revert.
4. **Lightsei's product surface now spans both audiences.** The customer's internal team uses the dashboard + Slack (Phases 16-20). The customer's end users interact through the widget (Phase 21). One trust-zone model, one capability model, one observability story.

## Cleanup

Delete the bot via `/agents/<name>` if you don't want to keep it deployed. Remove the embed snippet from any customer site you tested on. Clear the workspace's `widget_public_id` (no UI for this in v1; runs through a direct PATCH or DB poke if you really need to rotate it — most operators just leave it and rotate when they have a reason to).

## Notes

- The `support_bot.py` here uses heuristic FAQ + escalation logic for determinism. A real Halo support bot would swap this for an LLM call with retrieval over the actual docs.
- The `polaris.issue_pattern` events flow into `/polaris` alongside the 12D.2 cost-analysis events, so the operator's existing Polaris insights view picks up the widget patterns without code changes.
- The scan endpoint is operator-triggered in v1 (the "Scan for patterns" button). A future cron extension can run it on a schedule; the scaffolding is in place.
- Phase 21B parks: signed-token end-user identity + per-conversation PII redaction (lets the bot read user-specific data for the authenticated user only); SSE on `/inbox` instead of polling; inline widget embed (vs iframe); heuristic escalation (low-confidence, repeated follow-ups); embedding-based pattern clustering; widget theming.
