// Mirrors backend/agent_generator.py::STAR_DICTIONARY. Kept in sync by
// hand — there are only 20 entries and they rarely change. The
// inline-edit dropdown on this page validates name picks against this
// list; the backend's submit_team validator is the authoritative gate.
export const STAR_DICTIONARY: { name: string; theme: string }[] = [
  { name: "polaris", theme: "orchestration, navigation, the north star" },
  { name: "atlas", theme: "heavy/repeated work — tests, builds, batch jobs" },
  { name: "hermes", theme: "messenger — Slack, email, SMS, webhook" },
  { name: "argus", theme: "all-seeing — security, secret detection, audit" },
  { name: "vega", theme: "code review, PR scrutiny, structural critique" },
  { name: "sirius", theme: "alerting / on-call, the one that pages you" },
  { name: "cassiopeia", theme: "incident scribe, post-mortem writer" },
  { name: "lyra", theme: "harmony — coordination, cross-agent glue" },
  { name: "vela", theme: "deployment, shipping, release verification" },
  { name: "spica", theme: "summarization, digest, weekly recap" },
  { name: "rigel", theme: "infrastructure watcher, bedrock health" },
  { name: "antares", theme: "watching one critical thing closely" },
  { name: "altair", theme: "realtime streaming, low-latency reactions" },
  { name: "capella", theme: "fleet monitoring" },
  { name: "bellatrix", theme: "defensive guards, intrusion detection" },
  { name: "procyon", theme: "pre-commit hooks, pre-flight checks" },
  { name: "aldebaran", theme: "cleanup / sweep tasks downstream of others" },
  { name: "betelgeuse", theme: "long-running batch jobs, overnight work" },
  { name: "canopus", theme: "secondary backup, fallback agent" },
  { name: "arcturus", theme: "managing other bots' lifecycles" },
];

export const STAR_NAMES: Set<string> = new Set(
  STAR_DICTIONARY.map((s) => s.name),
);

export function isStarName(name: string): boolean {
  return STAR_NAMES.has(name.trim().toLowerCase());
}
