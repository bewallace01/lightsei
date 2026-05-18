# Coral

Coral is a B2B onboarding analytics platform. We help SaaS companies see where their new customers get stuck in product, then turn that into outreach + revenue.

We sell to Series A through D SaaS companies. ARR target $250k - $5M per customer. ~80 paying customers, ~$2.4M ARR, US + EU. Series A from Linden Capital in 2024.

## What Coral does

1. **Track**: a JS snippet customers paste into their product (Pendo-shaped). Captures user actions, time-on-task, drop-off in flows.
2. **Score**: per-account onboarding health score, refreshed daily. We grade activation, time-to-value, feature adoption depth.
3. **Trigger**: when a customer account's health crosses a threshold, fire an outreach playbook (email + Slack + task in the CSM's queue).

## How we work internally

We run on:

- **HubSpot CRM**. Source of truth for customer accounts, deals, contacts, owner assignments. Our customer success team lives in HubSpot all day.
- **Postgres**. Onboarding event data, scoring, internal dashboards.
- **Slack**. Internal comms + customer-facing shared channels with a subset of accounts.
- **Notion**. Playbook documentation, account briefs, kickoff templates.

Customer PII we store: name, email, phone, company, role, billing address (for invoiced customers). We're SOC 2 Type II as of Q1, GDPR-compliant. PII never leaves our SOC 2 boundary; vendor selection has been gated on this since founding.

## Where we want bots

Two areas where bots would save us hours per week:

### 1. Account-side automation

Pull a daily list of accounts in the "at risk" bucket (health score dropped >15 points week-over-week), enrich each with the latest HubSpot contact owner + last-touch timestamp + last-renewal date, write the digest into a CSM-team Slack channel, and open a draft outreach task in HubSpot for the account owner. Today this is a manual 45-minute slog every morning.

Sensitive: this touches every paying customer's account-level metadata and contact PII. Has to live entirely inside our SOC 2 boundary. No external API calls outside HubSpot + Slack.

### 2. Public-side research

When we're building a target list of prospects (e.g. "Series B SaaS in US, 50-200 employees, product-led growth"), we'd love a bot that combs LinkedIn, Crunchbase, public news, and writes us a list with company name, recent funding, headcount, recent product launches.

Not sensitive: this is all public information on companies we have no existing relationship with. PII concern is zero (it's public data on businesses, not individuals). External APIs and web fetching are fine.

These two workloads are NOT allowed to touch each other. The prospect-research bot must never see customer data; the customer-success bot must never make external network calls. If a CSM wants to enrich a customer account with public research, they (human) read the customer-side digest, decide what's safe to ask the research bot, and prompt the research bot manually. That handoff is intentional.

## Customer expectations

We sell into security-conscious mid-market. Every prospect asks about our data handling. Most ask about subprocessors. Several have asked specifically about how we handle internal AI/LLM use against their data. Our current answer is "we don't"; we'd like to upgrade that to "yes, and here are the architectural controls preventing leakage."

## What we don't want

- A general "AI agent" that has access to everything. Customer trust is our main asset; one PII leak loses 5+ accounts.
- A workflow where the prospect-research bot can read the at-risk-account digest and "helpfully" enrich it with public info on real customers.
- Anything that surfaces customer names, emails, or phone numbers in a tool that has internet access. Period.
