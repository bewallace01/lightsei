// Shared workspace-secret guidance used by both the /account page
// (reminders + "suggested secrets" panel) and the team-from-README
// deploy-success view (missing-secrets dropdown).
//
// Keep this short: one-line "what it is" + a hint about scopes /
// permissions when relevant. The link goes to the page where the user
// generates or copies the value.

export type SecretGuide = {
  what: string;
  where: string;
  url: string;
};

export const SECRET_GUIDANCE: Record<string, SecretGuide> = {
  ANTHROPIC_API_KEY: {
    what: "Used by bots that call Claude (Anthropic) for LLM responses.",
    where:
      "Anthropic Console → Settings → API Keys. Create a key, copy it (you only see it once), drop it on /account.",
    url: "https://console.anthropic.com/settings/keys",
  },
  OPENAI_API_KEY: {
    what: "Used by bots that call GPT / o-series models.",
    where:
      "OpenAI platform → API keys → Create new secret key. Project-scoped keys are fine; you only see the value once.",
    url: "https://platform.openai.com/api-keys",
  },
  GOOGLE_API_KEY: {
    what: "Used by bots that call Gemini.",
    where:
      "Google AI Studio → Get API key → Create API key in new project (or use an existing one).",
    url: "https://aistudio.google.com/apikey",
  },
  GITHUB_TOKEN: {
    what:
      "Used by bots that read or write to GitHub (PRs, issues, repo contents, push status).",
    where:
      "GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Grant only the repos + permissions the bots need (e.g. Contents: Read, Pull requests: Read & Write).",
    url: "https://github.com/settings/personal-access-tokens/new",
  },
  SLACK_WEBHOOK_URL: {
    what: "Posts messages to a single Slack channel.",
    where:
      "Slack → Apps → Incoming Webhooks → Add to Slack → pick the channel → copy the webhook URL (starts with https://hooks.slack.com/services/...).",
    url: "https://api.slack.com/messaging/webhooks",
  },
  DISCORD_WEBHOOK_URL: {
    what: "Posts messages to a single Discord channel.",
    where:
      "Discord channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL.",
    url: "https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
  },
  LIGHTSEI_API_KEY: {
    what:
      "Your workspace's own Lightsei key. Bots use it to send telemetry + check policies. Usually auto-provided on the bot's runtime, but list it here if you're running a bot outside Lightsei's worker.",
    where: "Lightsei → /account → API keys → Generate new key.",
    url: "/account",
  },
};

// Catch-all for whatever Claude decides to propose. Keeps the dropdown
// useful even when the secret name isn't in SECRET_GUIDANCE — better
// than a blank box.
export function guidanceFor(name: string): SecretGuide {
  const exact = SECRET_GUIDANCE[name];
  if (exact) return exact;
  return {
    what:
      "A workspace secret one of the proposed bots expects. Lightsei doesn't have a built-in template for this one.",
    where:
      "Check the relevant provider's API key / token page, copy the value, then add it on /account with this exact name.",
    url: "/account",
  };
}

// Stable display order for the /account suggested-secrets panel:
// LLM providers first, then VCS, then notifications, then Lightsei's
// own key last (since it's usually auto-provided).
export const SUGGESTED_SECRET_ORDER: string[] = [
  "ANTHROPIC_API_KEY",
  "OPENAI_API_KEY",
  "GOOGLE_API_KEY",
  "GITHUB_TOKEN",
  "SLACK_WEBHOOK_URL",
  "DISCORD_WEBHOOK_URL",
  "LIGHTSEI_API_KEY",
];
