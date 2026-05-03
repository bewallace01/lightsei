export const API_URL =
  process.env.NEXT_PUBLIC_LIGHTSEI_API_URL || "http://localhost:8000";

// Optional fallback bearer token baked at build time. Local docker compose
// sets this to "demo-key" so the spine demo works without logging in. In
// production it's empty: visitors must log in.
export const FALLBACK_API_KEY = process.env.NEXT_PUBLIC_LIGHTSEI_API_KEY || "";

const SESSION_KEY = "lightsei.session_token";
const USER_KEY = "lightsei.user";
const WORKSPACE_KEY = "lightsei.workspace";

export class UnauthorizedError extends Error {
  constructor(message = "unauthorized") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

export type SessionUser = {
  id: string;
  email: string;
  workspace_id: string;
};

export type SessionWorkspace = {
  id: string;
  name: string;
};

export function getSessionToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(SESSION_KEY);
}

export function setSession(
  token: string,
  user: SessionUser,
  workspace: SessionWorkspace,
): void {
  localStorage.setItem(SESSION_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
  localStorage.setItem(WORKSPACE_KEY, JSON.stringify(workspace));
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(WORKSPACE_KEY);
}

export function getStoredUser(): SessionUser | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(USER_KEY);
  return raw ? (JSON.parse(raw) as SessionUser) : null;
}

export function getStoredWorkspace(): SessionWorkspace | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(WORKSPACE_KEY);
  return raw ? (JSON.parse(raw) as SessionWorkspace) : null;
}

function authHeaders(): Record<string, string> {
  const token = getSessionToken() || FALLBACK_API_KEY;
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

export type Run = {
  id: string;
  agent_name: string;
  started_at: string;
  ended_at: string | null;
};

export type Event = {
  id: number;
  run_id: string;
  agent_name: string;
  kind: string;
  payload: Record<string, unknown>;
  timestamp: string;
};

export type Denial = {
  policy?: string;
  reason?: string;
  cap_usd?: number;
  cost_so_far_usd?: number;
  action?: string;
};

export type RunSummary = Run & {
  model?: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  event_count: number;
  denied: boolean;
  denial?: Denial;
};

export async function fetchRuns(): Promise<Run[]> {
  const r = await fetch(`${API_URL}/runs`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error(`/runs returned ${r.status}`);
  const body = (await r.json()) as { runs: Run[] };
  return body.runs;
}

export async function fetchRunEvents(
  runId: string,
): Promise<{ run: Run; events: Event[] }> {
  const r = await fetch(`${API_URL}/runs/${runId}/events`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error(`/runs/${runId}/events returned ${r.status}`);
  return (await r.json()) as { run: Run; events: Event[] };
}

export function summarize(run: Run, events: Event[]): RunSummary {
  let model: string | undefined;
  let input_tokens = 0;
  let output_tokens = 0;
  let latency_ms = 0;
  let denial: Denial | undefined;

  for (const e of events) {
    if (e.kind === "llm_call_completed") {
      const p = e.payload as {
        model?: string;
        input_tokens?: number;
        output_tokens?: number;
        duration_s?: number;
      };
      if (p.model) model = p.model;
      input_tokens += p.input_tokens ?? 0;
      output_tokens += p.output_tokens ?? 0;
      if (typeof p.duration_s === "number") latency_ms += p.duration_s * 1000;
    } else if (e.kind === "policy_denied" && !denial) {
      const p = e.payload as Denial;
      denial = {
        policy: p.policy,
        reason: p.reason,
        cap_usd: p.cap_usd,
        cost_so_far_usd: p.cost_so_far_usd,
        action: p.action,
      };
    }
  }

  return {
    ...run,
    model,
    input_tokens,
    output_tokens,
    latency_ms: Math.round(latency_ms),
    event_count: events.length,
    denied: denial !== undefined,
    denial,
  };
}

export async function fetchRunSummaries(): Promise<RunSummary[]> {
  const runs = await fetchRuns();
  return Promise.all(
    runs.map(async (r) => {
      try {
        const { events } = await fetchRunEvents(r.id);
        return summarize(r, events);
      } catch {
        return summarize(r, []);
      }
    }),
  );
}

export async function signup(
  email: string,
  password: string,
  workspaceName: string,
): Promise<{
  user: SessionUser;
  workspace: SessionWorkspace;
  api_key: { plaintext: string; prefix: string };
  session_token: string;
}> {
  const r = await fetch(`${API_URL}/auth/signup`, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      email,
      password,
      workspace_name: workspaceName,
    }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `signup failed (${r.status})`);
  }
  return await r.json();
}

export async function login(
  email: string,
  password: string,
): Promise<{
  user: SessionUser;
  workspace: SessionWorkspace;
  session_token: string;
}> {
  const r = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `login failed (${r.status})`);
  }
  return await r.json();
}

export async function logout(): Promise<void> {
  const token = getSessionToken();
  if (!token) return;
  try {
    await fetch(`${API_URL}/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
  } finally {
    clearSession();
  }
}

// ----- /account page helpers -----

export type ApiKeySummary = {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

export type SessionSummary = {
  id: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  current: boolean;
};

async function authedJson(
  path: string,
  init?: RequestInit,
): Promise<unknown> {
  const r = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      ...authHeaders(),
      ...(init?.headers || {}),
    },
  });
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(
      typeof body === "object" && body && "detail" in body
        ? String((body as { detail: string }).detail)
        : `${path} returned ${r.status}`,
    );
  }
  return r.json();
}

export async function fetchWorkspace(): Promise<SessionWorkspace> {
  return (await authedJson("/workspaces/me")) as SessionWorkspace;
}

export async function renameWorkspace(name: string): Promise<SessionWorkspace> {
  return (await authedJson("/workspaces/me", {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  })) as SessionWorkspace;
}

export async function fetchApiKeys(): Promise<ApiKeySummary[]> {
  const body = (await authedJson("/workspaces/me/api-keys")) as {
    api_keys: ApiKeySummary[];
  };
  return body.api_keys;
}

export async function createApiKey(name: string): Promise<ApiKeySummary & { plaintext: string }> {
  return (await authedJson("/workspaces/me/api-keys", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  })) as ApiKeySummary & { plaintext: string };
}

export async function revokeApiKey(keyId: string): Promise<ApiKeySummary> {
  return (await authedJson(`/workspaces/me/api-keys/${keyId}`, {
    method: "DELETE",
  })) as ApiKeySummary;
}

export async function fetchSessions(): Promise<SessionSummary[]> {
  const body = (await authedJson("/auth/sessions")) as {
    sessions: SessionSummary[];
  };
  return body.sessions;
}

export async function revokeSession(sessionId: string): Promise<SessionSummary> {
  return (await authedJson(`/auth/sessions/${sessionId}`, {
    method: "DELETE",
  })) as SessionSummary;
}

// ----- Workspace secrets -----

export type WorkspaceSecretMeta = {
  name: string;
  created_at: string;
  updated_at: string;
};

export async function fetchSecrets(): Promise<WorkspaceSecretMeta[]> {
  const body = (await authedJson("/workspaces/me/secrets")) as {
    secrets: WorkspaceSecretMeta[];
  };
  return body.secrets;
}

export async function setSecret(
  name: string,
  value: string,
): Promise<WorkspaceSecretMeta> {
  return (await authedJson(`/workspaces/me/secrets/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value }),
  })) as WorkspaceSecretMeta;
}

export async function deleteSecret(name: string): Promise<void> {
  await authedJson(`/workspaces/me/secrets/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ----- Commands -----

export type Command = {
  id: string;
  agent_name: string;
  kind: string;
  payload: Record<string, unknown>;
  status: "pending" | "claimed" | "completed" | "failed" | "cancelled";
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  claimed_at: string | null;
  completed_at: string | null;
  expires_at: string;
};

export async function fetchCommands(agentName: string): Promise<Command[]> {
  const body = (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/commands`,
  )) as { commands: Command[] };
  return body.commands;
}

export async function enqueueCommand(
  agentName: string,
  kind: string,
  payload: Record<string, unknown>,
): Promise<Command> {
  return (await authedJson(`/agents/${encodeURIComponent(agentName)}/commands`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, payload }),
  })) as Command;
}

export async function cancelCommand(commandId: string): Promise<Command> {
  return (await authedJson(`/commands/${commandId}`, {
    method: "DELETE",
  })) as Command;
}

export type CommandHandler = {
  kind: string;
  description: string | null;
};

export type AgentManifest = {
  agent_name: string;
  command_handlers: CommandHandler[];
  last_seen_at: string | null;
};

export type Agent = {
  name: string;
  daily_cost_cap_usd: number | null;
  system_prompt: string | null;
  created_at: string;
  updated_at: string;
};

export async function fetchAgent(name: string): Promise<Agent> {
  return (await authedJson(
    `/agents/${encodeURIComponent(name)}`,
  )) as Agent;
}

export async function patchAgent(
  name: string,
  patch: Partial<{ daily_cost_cap_usd: number | null; system_prompt: string | null }>,
): Promise<Agent> {
  return (await authedJson(`/agents/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  })) as Agent;
}

export async function fetchAgentManifest(agentName: string): Promise<AgentManifest> {
  return (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/manifest`,
  )) as AgentManifest;
}

export type AgentInstance = {
  id: string;
  agent_name: string;
  hostname: string | null;
  pid: number | null;
  sdk_version: string | null;
  started_at: string;
  last_heartbeat_at: string;
  status: "active" | "stale";
};

export async function fetchAgentInstances(
  agentName: string,
): Promise<AgentInstance[]> {
  const body = (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/instances`,
  )) as { instances: AgentInstance[] };
  return body.instances;
}

// ----- Deployments (Phase 5.4 + 5.6) -----

export type Deployment = {
  id: string;
  agent_name: string;
  status: "queued" | "building" | "running" | "stopped" | "failed";
  desired_state: "running" | "stopped";
  source_blob_id: string | null;
  // Phase 10.3: provenance. "cli" means an SDK upload via
  // POST /workspaces/me/deployments; "github_push" means a webhook
  // landed at /webhooks/github and the backend fetched the agent
  // dir at source_commit_sha. source_commit_sha is null for cli.
  source: "cli" | "github_push";
  source_commit_sha: string | null;
  error: string | null;
  claimed_by: string | null;
  claimed_at: string | null;
  heartbeat_at: string | null;
  started_at: string | null;
  stopped_at: string | null;
  created_at: string;
  updated_at: string;
};

export type DeploymentLogLine = {
  id: number;
  ts: string;
  stream: "stdout" | "stderr" | "system";
  line: string;
};

export async function fetchDeployments(
  agentName?: string,
): Promise<Deployment[]> {
  const qs = agentName
    ? `?agent_name=${encodeURIComponent(agentName)}`
    : "";
  const body = (await authedJson(`/workspaces/me/deployments${qs}`)) as {
    deployments: Deployment[];
  };
  return body.deployments;
}

export async function fetchDeployment(id: string): Promise<Deployment> {
  return (await authedJson(
    `/workspaces/me/deployments/${id}`,
  )) as Deployment;
}

export async function fetchDeploymentLogs(
  id: string,
  afterId = 0,
  limit = 200,
): Promise<{ lines: DeploymentLogLine[]; max_id: number }> {
  return (await authedJson(
    `/workspaces/me/deployments/${id}/logs?after_id=${afterId}&limit=${limit}`,
  )) as { lines: DeploymentLogLine[]; max_id: number };
}

export async function stopDeployment(id: string): Promise<Deployment> {
  return (await authedJson(`/workspaces/me/deployments/${id}/stop`, {
    method: "POST",
  })) as Deployment;
}

export async function redeployDeployment(id: string): Promise<Deployment> {
  return (await authedJson(`/workspaces/me/deployments/${id}/redeploy`, {
    method: "POST",
  })) as Deployment;
}

export async function deleteDeployment(id: string): Promise<void> {
  await authedJson(`/workspaces/me/deployments/${id}`, { method: "DELETE" });
}

// ----- Chat -----

export type Thread = {
  id: string;
  agent_name: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type ThreadMessage = {
  id: string;
  thread_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  status: "completed" | "pending" | "in_progress" | "failed";
  error: string | null;
  created_at: string;
  completed_at: string | null;
};

export async function listThreads(agentName: string): Promise<Thread[]> {
  const body = (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/threads`,
  )) as { threads: Thread[] };
  return body.threads;
}

export async function createThread(
  agentName: string,
  title?: string,
): Promise<Thread> {
  return (await authedJson(`/agents/${encodeURIComponent(agentName)}/threads`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title }),
  })) as Thread;
}

export async function getThread(
  threadId: string,
): Promise<{ thread: Thread; messages: ThreadMessage[] }> {
  return (await authedJson(`/threads/${threadId}`)) as {
    thread: Thread;
    messages: ThreadMessage[];
  };
}

export async function postThreadMessage(
  threadId: string,
  content: string,
): Promise<{ user_message: ThreadMessage; pending_message: ThreadMessage }> {
  return (await authedJson(`/threads/${threadId}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ content }),
  })) as { user_message: ThreadMessage; pending_message: ThreadMessage };
}

export async function deleteThread(threadId: string): Promise<void> {
  await authedJson(`/threads/${threadId}`, { method: "DELETE" });
}

// ---------- Polaris (Phase 6.4) ---------- //

export type PolarisNextAction = {
  task: string;
  why: string;
  blocked_by: string | null;
};

export type PolarisPromotion = {
  item: string;
  why: string;
};

export type PolarisDrift = {
  between: string;
  observation: string;
};

export type PolarisPlanPayload = {
  // Most fields are nominally required but marked optional here so the
  // dashboard renders gracefully when a non-conforming polaris.plan
  // event lands (e.g., a manual /events POST during testing, or a
  // schema-rejected payload from before 8.2 went blocking). The page
  // handles missing fields via `?? defaults` rather than crashing.
  text?: string;
  doc_hashes?: { memory_md?: string; tasks_md?: string };
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  // present on successful parse
  summary?: string;
  next_actions?: PolarisNextAction[];
  parking_lot_promotions?: PolarisPromotion[];
  drift?: PolarisDrift[];
  // present on parse failure
  parse_error?: string;
};

// Phase 7 validation. Status values match backend/validation_pipeline.py:
//   pass    | every validator returned ok=True with no violations
//   warn    | only warn-severity violations (advisory)
//   fail    | at least one fail-severity violation
//   error   | validator function raised, or config references an
//             unknown validator (registry mismatch)
//   timeout | cumulative validator budget exceeded; remaining skipped
export type ValidationStatus = "pass" | "fail" | "warn" | "error" | "timeout";

export type PolarisViolation = {
  rule: string;
  message: string;
  // Validator-specific extras. Both undefined on plain violations.
  path?: string;        // schema_strict: JSON pointer to the field
  matched?: string;     // content_rules: matched substring (redacted by validator)
  severity?: "fail" | "warn";  // content_rules
};

// Two shapes of validation depending on which endpoint produced it. The
// /agents/{name}/plans list endpoint returns lite summaries (chip-sized);
// /agents/{name}/latest-plan and /events/{id}/validations return full
// details. Treated as a single optional-fields type so the rendering code
// can branch on which fields are present.
export type PolarisValidation = {
  validator: string;
  status: ValidationStatus;
  violations?: PolarisViolation[];  // present on full responses
  violation_count?: number;          // present on lite (list) responses
};

export type PolarisPlan = {
  event_id: number;
  run_id: string;
  agent_name: string;
  timestamp: string;
  payload: PolarisPlanPayload;
  validations?: PolarisValidation[];  // present from Phase 7.4 onward
};

/** Worst status across a set of validations. Drives the sidebar chip. */
export function worstValidationStatus(
  validations: PolarisValidation[] | undefined,
): ValidationStatus | "unchecked" {
  if (!validations || validations.length === 0) return "unchecked";
  const order: Record<ValidationStatus, number> = {
    pass: 0,
    warn: 1,
    timeout: 2,
    error: 3,
    fail: 4,
  };
  let worst: ValidationStatus = "pass";
  for (const v of validations) {
    if (order[v.status] > order[worst]) worst = v.status;
  }
  return worst;
}

export async function fetchLatestPolarisPlan(
  agentName: string,
): Promise<PolarisPlan | null> {
  const r = await fetch(
    `${API_URL}/agents/${encodeURIComponent(agentName)}/latest-plan`,
    { cache: "no-store", headers: authHeaders() },
  );
  if (r.status === 401) throw new UnauthorizedError();
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`/latest-plan returned ${r.status}`);
  return (await r.json()) as PolarisPlan;
}

export async function fetchPolarisPlans(
  agentName: string,
  limit = 20,
): Promise<PolarisPlan[]> {
  const body = (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/plans?limit=${limit}`,
  )) as { plans: PolarisPlan[] };
  return body.plans;
}

/** Full violation details for a single event. Used when the user clicks a
 *  historical plan in the sidebar — the list endpoint only ships
 *  summaries, so we lazy-load full violations on selection. */
export async function fetchEventValidations(
  eventId: number,
): Promise<PolarisValidation[]> {
  const body = (await authedJson(`/events/${eventId}/validations`)) as {
    event_id: number;
    validations: PolarisValidation[];
  };
  return body.validations;
}

// ---------- Notifications (Phase 9.5) ---------- //

// Pinned to the backend's NOTIFICATION_CHANNEL_TYPES + TRIGGERS lists.
// Renaming any of these requires a coordinated backend migration.
export type ChannelType =
  | "slack"
  | "discord"
  | "teams"
  | "mattermost"
  | "webhook";

export type TriggerName = "polaris.plan" | "validation.fail" | "run_failed";

export type NotificationChannel = {
  id: string;
  name: string;
  type: ChannelType;
  // The backend masks the URL — Slack-style "https://hooks.slack.com/services/T01...XXXX"
  // for native chat, full mask for generic webhooks. Never the full URL.
  target_url_masked: string;
  triggers: TriggerName[];
  has_secret_token: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type NotificationDelivery = {
  id: number;
  channel_id: string;
  event_id: number | null;
  trigger: string;
  // 'sent' | 'failed' on the happy path; 'skipped' from old (pre-9.2)
  // rows; 'error'/'timeout' from defensive paths.
  status: string;
  response_summary: Record<string, unknown>;
  attempt_count: number;
  sent_at: string;
};

export async function fetchNotificationChannels(): Promise<NotificationChannel[]> {
  const body = (await authedJson(`/workspaces/me/notifications`)) as {
    channels: NotificationChannel[];
  };
  return body.channels;
}

export async function createNotificationChannel(input: {
  name: string;
  type: ChannelType;
  target_url: string;
  triggers: TriggerName[];
  secret_token?: string;
  is_active?: boolean;
}): Promise<NotificationChannel> {
  return (await authedJson(`/workspaces/me/notifications`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as NotificationChannel;
}

export async function patchNotificationChannel(
  id: string,
  patch: Partial<{
    name: string;
    target_url: string;
    triggers: TriggerName[];
    secret_token: string | null;
    is_active: boolean;
  }>,
): Promise<NotificationChannel> {
  return (await authedJson(`/workspaces/me/notifications/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  })) as NotificationChannel;
}

export async function deleteNotificationChannel(id: string): Promise<void> {
  await authedJson(`/workspaces/me/notifications/${id}`, { method: "DELETE" });
}

/** Fire a synthetic test message immediately. Returns the resulting
 *  NotificationDelivery row so the UI can show "✓ sent" or
 *  "✗ failed: 401" inline. */
export async function testNotificationChannel(
  id: string,
): Promise<NotificationDelivery> {
  const body = (await authedJson(
    `/workspaces/me/notifications/${id}/test`,
    { method: "POST" },
  )) as { delivery: NotificationDelivery };
  return body.delivery;
}

export async function fetchNotificationDeliveries(
  id: string,
  limit = 50,
): Promise<NotificationDelivery[]> {
  const body = (await authedJson(
    `/workspaces/me/notifications/${id}/deliveries?limit=${limit}`,
  )) as { deliveries: NotificationDelivery[] };
  return body.deliveries;
}

// ---------- GitHub integration (Phase 10.5) ---------- //
//
// One repo per workspace in v1. The PUT response is the only surface
// where webhook_secret comes back in plaintext — store it in component
// state so the user can copy it once, and surface a "to rotate, delete
// + re-register" affordance for later. Subsequent GET calls return
// only `has_webhook_secret: true`.

export type GitHubIntegration = {
  id: string;
  repo_owner: string;
  repo_name: string;
  branch: string;
  pat_masked: string;
  has_webhook_secret: boolean;
  is_active: boolean;
  webhook_url: string;
  created_at: string;
  updated_at: string;
};

export type GitHubIntegrationFresh = GitHubIntegration & {
  // Only present on the response from PUT (initial registration).
  // After that the secret stays encrypted at rest and the user must
  // delete + re-register to see a new one.
  webhook_secret?: string;
  webhook_secret_reveal_note?: string;
  default_branch_from_github?: string;
};

export type GitHubAgentPath = {
  agent_name: string;
  path: string;
  created_at: string;
  updated_at: string;
};

export async function fetchGitHubIntegration(): Promise<GitHubIntegration | null> {
  // 404 is the load-bearing "no integration registered" signal — swallow
  // it before the generic authedJson error path turns it into a thrown
  // Error with a string detail.
  const r = await fetch(`${API_URL}/workspaces/me/github`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (r.status === 401) throw new UnauthorizedError();
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`/workspaces/me/github returned ${r.status}`);
  return (await r.json()) as GitHubIntegration;
}

export async function putGitHubIntegration(input: {
  repo_owner: string;
  repo_name: string;
  branch: string;
  pat: string;
}): Promise<GitHubIntegrationFresh> {
  return (await authedJson("/workspaces/me/github", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as GitHubIntegrationFresh;
}

export async function deleteGitHubIntegration(): Promise<void> {
  await authedJson("/workspaces/me/github", { method: "DELETE" });
}

export async function listGitHubAgentPaths(): Promise<GitHubAgentPath[]> {
  const body = (await authedJson(
    "/workspaces/me/github/agents",
  )) as { agents: GitHubAgentPath[] };
  return body.agents;
}

export async function putGitHubAgentPath(
  agentName: string,
  path: string,
): Promise<GitHubAgentPath> {
  return (await authedJson(
    `/workspaces/me/github/agents/${encodeURIComponent(agentName)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path }),
    },
  )) as GitHubAgentPath;
}

export async function deleteGitHubAgentPath(agentName: string): Promise<void> {
  await authedJson(
    `/workspaces/me/github/agents/${encodeURIComponent(agentName)}`,
    { method: "DELETE" },
  );
}

export async function fetchAgents(): Promise<Agent[]> {
  const body = (await authedJson("/agents")) as { agents: Agent[] };
  return body.agents;
}

// ---------- Constellation map (Phase 11B.3) ---------- //
//
// Drives the home-page constellation widget. agents are nodes; edges are
// dispatch relationships. Polled every 5s by the home page (paused when
// the tab is hidden). Edges stay empty until Phase 11.2's dispatch_chain
// machinery lands; the contract is stable now so the widget doesn't
// need to change when edges fill in.

export type ConstellationAgent = {
  name: string;
  role: "orchestrator" | "executor" | "notifier" | "specialist";
  model: string | null;
  status: "active" | "stale" | "stopped";
  runs_24h: number;
  cost_24h_usd: number;
  last_event_at: string | null;
  last_heartbeat_at: string | null;
};

export type ConstellationEdge = {
  from: string;
  to: string;
  count_24h: number;
  last_at: string | null;
};

export type ConstellationData = {
  agents: ConstellationAgent[];
  edges: ConstellationEdge[];
};

export async function fetchConstellation(): Promise<ConstellationData> {
  return (await authedJson(
    "/workspaces/me/constellation",
  )) as ConstellationData;
}

// ---------- Workspace cost telemetry (Phase 11B.1) ---------- //

export type WorkspaceCostByAgent = {
  agent_name: string;
  mtd_usd: number;
  run_count: number;
  last_run_at: string | null;
};

export type WorkspaceCostByModel = {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  mtd_usd: number;
};

export type WorkspaceCost = {
  mtd_usd: number;
  projected_eom_usd: number;
  run_count: number;
  by_agent: WorkspaceCostByAgent[];
  by_model: WorkspaceCostByModel[];
  budget_usd_monthly: number | null;
  budget_used_pct: number | null;
  month_start: string;
  as_of: string;
};

export async function fetchWorkspaceCost(): Promise<WorkspaceCost> {
  return (await authedJson("/workspaces/me/cost")) as WorkspaceCost;
}
