# Halo (the build-monitoring product)

Halo is a SaaS product for engineering teams. We watch your CI, surface flaky tests + slow stages + cost regressions, and route fixes to whoever owns the broken thing.

We sell into Series A through D engineering orgs. Free up to 5 repos; paid plans start at $400/month.

## What Halo does

1. Connect Halo to your CI provider (GitHub Actions, CircleCI, Buildkite).
2. We watch every push + every nightly build. When a test flakes or a stage gets slower, we surface it.
3. The dashboard shows you the team-level + per-repo view; alerts go to Slack or PagerDuty.

## Where customer questions usually come from

We've shipped enough that we have a real volume of recurring customer questions. The customer success team triages them today; we want to automate the easy ones.

The top three buckets in the last quarter, sorted by volume:

1. **Pricing + plan questions.** "What's included in the Pro plan?" "Do you charge per seat or per repo?" "What happens when I exceed the build-minute limit?" Plain answers; our pricing page has all this.
2. **Integration setup help.** "How do I connect Halo to my GitHub Actions repo?" "Where do I paste the API token?" "Does Halo work with self-hosted GitLab?" Step-by-step. We have docs but customers don't always find them.
3. **Account-specific issues.** "My builds aren't showing up." "The dashboard says 'no data' for repo X." "I can't see the Slack alerts I expected." These usually require a human — they involve looking at the specific customer's account state.

## Where we want a customer-facing bot

A bot embedded on our docs site + our product's marketing pages. End user (someone visiting halo.dev) types a question into a chat widget; the bot answers.

The bot should:

- Confidently answer pricing + plan questions from our published pricing page.
- Walk users through the basic integration-setup flow for GitHub Actions, CircleCI, and Buildkite.
- Recognize when a question is account-specific (anything that requires looking up a customer's repo / dashboard state) + escalate to a human.

The bot lives in the `public` trust zone. It cannot see customer account data, can't read internal docs, can't touch our HubSpot CRM. If it tries to, the framework refuses the call — that's the wedge.

## What's off-limits

This is a `public` bot, so:

- No customer-specific lookups. End users aren't authenticated; the bot doesn't know who they are.
- No internal/sensitive/PII connectors. Even if an operator misconfigured capabilities, the trust-zone gate refuses.
- No outbound internet calls. Bot answers from the system prompt + escalates when stuck.

If a customer needs an account-specific answer, the bot escalates to the operator inbox. A real human picks it up there.
