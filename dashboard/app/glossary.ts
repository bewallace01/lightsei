// Phase 18.7: glossary of the technical terms surfaced in the
// dashboard. Each entry feeds the <HelpTip> component — a 1-2 line
// description that opens on hover, plus an optional docs link.
//
// Add new entries when a term lands somewhere a non-technical user
// will hit. Keep descriptions short — long-form explanations go in
// docs (under the Advanced nav dropdown). The goal is "answer the
// question 'what does this mean?' in 6 seconds without leaving the
// page," not "teach the concept."

export type GlossaryEntry = {
  term: string;
  description: string;
  docsHref?: string;
};

// Explicitly typed as Record so lookups yield the wider GlossaryEntry
// shape (with optional docsHref); `satisfies` alone narrows per-key
// types and breaks the HelpTip component's `entry.docsHref` access on
// keys whose entries don't declare a docsHref.
export const GLOSSARY: Record<string, GlossaryEntry> = {
  sensitivity_zone: {
    term: "Sensitivity zone",
    description:
      "Where this bot's data sits on a 4-step ladder: public, internal, sensitive, pii. The framework refuses outbound calls and cross-zone dispatches based on this level. pii bots have no internet by default; public bots can reach the web.",
    docsHref: "/getting-started#trust-zones",
  },
  capability: {
    term: "Capability",
    description:
      "An explicit allow-list of outbound operations this bot is permitted to do (e.g. 'internet', 'send_command'). Default-deny: anything not on the list is refused by the SDK before the network call.",
    docsHref: "/getting-started#capabilities",
  },
  cross_zone_dispatch: {
    term: "Cross-zone dispatch",
    description:
      "When a bot tries to send a command to another bot in a different sensitivity zone. Refused by default; only allowed when the source bot has dispatches_cross_zone=True explicitly enabled (the operator opted in).",
    docsHref: "/getting-started#cross-zone",
  },
  dispatch_chain: {
    term: "Dispatch chain",
    description:
      "The cause-and-effect tree of commands one bot sent another. Polaris → Atlas → Hermes is one chain; Lightsei groups them so you can debug a multi-bot workflow as one unit.",
  },
  quality_signal: {
    term: "Quality signal",
    description:
      "A judge LLM samples your bot's recent runs and grades each on whether it actually completed the task. The chip shows the % of recent runs the judge graded 'good' (green) or 'bad' (red).",
    docsHref: "/getting-started#quality",
  },
  verdict: {
    term: "Verdict",
    description:
      "The judge LLM's per-run grade: good, borderline, or bad. The judge sees the bot's input + output and decides whether the output actually addresses the input.",
  },
  workspace_secret: {
    term: "Workspace secret",
    description:
      "Encrypted KV store for API keys, webhook URLs, and other config your bot needs at runtime. Read from bot code with lightsei.get_secret('NAME').",
    docsHref: "/getting-started#secrets",
  },
  command_kind: {
    term: "Command kind",
    description:
      "A bot's symbolic handler name, like 'argus.scan' or 'hermes.post'. Convention: <agent_name>.<verb>. When you send a command, Lightsei routes it to whichever bot registered that kind.",
  },
  orchestrator: {
    term: "Orchestrator",
    description:
      "A coordinator bot — typically cron-scheduled, reads workspace docs, and dispatches commands to specialist bots. Polaris is the canonical orchestrator. Usually one per workspace.",
  },
  specialist: {
    term: "Specialist",
    description:
      "A bot that does specific work (review code, scan for secrets, enrich a CRM record). Receives commands from the orchestrator or other specialists; emits results.",
  },
  messenger: {
    term: "Messenger",
    description:
      "A leaf-node bot that posts outbound (Slack, email, webhook). Receives a command, formats the payload, sends it. Hermes is the canonical messenger.",
  },
  handoff_span: {
    term: "Handoff span",
    description:
      "A logged human-mediated translation between two bot runs across a zone boundary. The operator reads one bot's output, decides what's safe to forward, types a sanitized prompt to the next bot — and lightsei.handoff_span links the two runs in the trace view.",
    docsHref: "/getting-started#handoff",
  },
};

// Stable union of known glossary keys for the <HelpTip term="..."> prop.
// Hard-coded so a typo in the term name is a compile error, not a
// silent fallback to undefined.
export type GlossaryKey =
  | "sensitivity_zone"
  | "capability"
  | "cross_zone_dispatch"
  | "dispatch_chain"
  | "quality_signal"
  | "verdict"
  | "workspace_secret"
  | "command_kind"
  | "orchestrator"
  | "specialist"
  | "messenger"
  | "handoff_span";
