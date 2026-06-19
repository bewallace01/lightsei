import {
  clearEndUserSession,
  getEndUserSessionToken,
} from "./endUserSession";

export const API_URL =
  process.env.NEXT_PUBLIC_LIGHTSEI_API_URL || "http://localhost:8000";

// Optional fallback bearer token baked at build time. Local docker compose
// sets this to "demo-key" so the spine demo works without logging in. In
// production it's empty: visitors must log in.
export const FALLBACK_API_KEY = process.env.NEXT_PUBLIC_LIGHTSEI_API_KEY || "";

const SESSION_KEY = "lightsei.session_token";
const USER_KEY = "lightsei.user";
const WORKSPACE_KEY = "lightsei.workspace";

// Phase 26.3: auth fallback chain.
//
// /c/* consumer routes use end-user auth via `endUserAuthedJson` +
// the helpers exported from `./endUserSession`.
//
// Everything else (operator dashboard) uses `authedJson` + the
// SESSION_KEY-backed operator helpers below.
//
// The two storage keys are distinct (`lightsei.end_user_session`
// vs `lightsei.session_token`) so a signed-in operator browsing
// /c with a separate end-user account doesn't bleed credentials
// across surfaces.

export class UnauthorizedError extends Error {
  constructor(message = "unauthorized") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

// Phase 23.6: distinct subclass for the case where the user IS
// authenticated but their session's active workspace is unset or
// stale. Pages catch this separately and route to the picker page
// (/me/workspaces/pick) instead of /login — the user isn't logged
// out, they just need to pick a workspace to continue.
export class NoActiveWorkspaceError extends UnauthorizedError {
  constructor(message = "no active workspace") {
    super(message);
    this.name = "NoActiveWorkspaceError";
  }
}

// Phase 23.x (#227): shared auth-error router so every page's catch
// block routes the same way without re-implementing the
// NoActiveWorkspaceError-before-UnauthorizedError check. Returns
// true if it handled the error (caller should bail out); false
// otherwise so the caller can fall through to whatever else it
// does (setError, log, etc).
//
// Accepts a structurally-typed `router` (just needs .replace) so
// api.ts stays free of next/navigation imports.
export function handleAuthError(
  e: unknown,
  router: { replace: (path: string) => void },
): boolean {
  if (e instanceof NoActiveWorkspaceError) {
    router.replace("/me/workspaces/pick");
    return true;
  }
  if (e instanceof UnauthorizedError) {
    router.replace("/login");
    return true;
  }
  return false;
}

// Detail strings the backend's auth.py (Phase 23.2) returns when the
// session is valid but the workspace context isn't. Keep these in
// sync with the backend's literal HTTPException details.
const _NO_ACTIVE_DETAILS = new Set([
  "no active workspace",
  "not a member of active workspace",
]);

export type SessionUser = {
  id: string;
  email: string;
  workspace_id: string;
};

export type SessionWorkspace = {
  id: string;
  name: string;
  // Phase 17.7: billing fields surfaced by the backend's
  // _serialize_workspace. Optional so older cached values in
  // localStorage still parse cleanly.
  plan_tier?: "free" | "paid";
  free_credits_remaining_usd?: number;
  has_stripe_customer?: boolean;
  budget_usd_monthly?: number | null;
  created_at?: string;
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

// Phase 23.x (#218): patch just the workspace bucket of session
// storage without re-setting the token + user. The
// WorkspaceSwitcher calls this after a switch/create so the
// Header chip + dropdown title catch up without a page reload.
// Billing-shaped fields (free_credits, has_stripe_customer, etc.)
// not present on the WorkspaceMembership response get refreshed
// next time the dashboard calls fetchWorkspace().
export function setStoredWorkspace(workspace: SessionWorkspace): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(WORKSPACE_KEY, JSON.stringify(workspace));
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
  // Phase 22.8: trigger link. Null on manual runs. `trigger_kind` is
  // a snapshot so a deleted trigger still surfaces the badge.
  triggered_by_trigger_id?: string | null;
  trigger_kind?: string | null;
  trigger_name?: string | null;
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

export async function fetchRuns(
  options?: { triggerId?: string },
): Promise<Run[]> {
  const qs = new URLSearchParams();
  if (options?.triggerId) qs.set("trigger_id", options.triggerId);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const r = await fetch(`${API_URL}/runs${suffix}`, {
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

// ---------- Phase 15.4: behavioral rules (guardrail layer 4) ---------- //

export type BehavioralViolation = {
  rule: string;
  severity: "warn" | "block";
  reason: string;
  details: Record<string, unknown>;
  created_at: string;
};

export type RunBehavior = {
  run_id: string;
  worst_severity: "none" | "warn" | "block";
  violations: BehavioralViolation[];
};

export async function fetchRunBehavior(runId: string): Promise<RunBehavior> {
  const r = await fetch(`${API_URL}/runs/${runId}/behavior`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error(`/runs/${runId}/behavior returned ${r.status}`);
  return (await r.json()) as RunBehavior;
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

export async function fetchRunSummaries(
  options?: { triggerId?: string },
): Promise<RunSummary[]> {
  const runs = await fetchRuns(options);
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

// ---------- Phase 17.7: Stripe billing helpers ---------- //

export class BillingNotConfiguredError extends Error {
  constructor(message = "billing is not configured on this backend") {
    super(message);
    this.name = "BillingNotConfiguredError";
  }
}

export async function createBillingCheckout(): Promise<{
  checkout_url: string;
  session_id: string;
}> {
  const r = await fetch(`${API_URL}/workspaces/me/billing/checkout`, {
    method: "POST",
    cache: "no-store",
    headers: { ...authHeaders(), "content-type": "application/json" },
  });
  if (r.status === 503) {
    throw new BillingNotConfiguredError();
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = (body as { detail?: unknown })?.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      throw new Error(String((detail as { message: string }).message));
    }
    throw new Error(
      typeof detail === "string"
        ? detail
        : `billing checkout failed (${r.status})`,
    );
  }
  return await r.json();
}

export async function createBillingPortal(): Promise<{
  portal_url: string;
  session_id: string;
}> {
  const r = await fetch(`${API_URL}/workspaces/me/billing/portal`, {
    method: "POST",
    cache: "no-store",
    headers: { ...authHeaders(), "content-type": "application/json" },
  });
  if (r.status === 503) {
    throw new BillingNotConfiguredError();
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = (body as { detail?: unknown })?.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      throw new Error(String((detail as { message: string }).message));
    }
    throw new Error(
      typeof detail === "string"
        ? detail
        : `billing portal failed (${r.status})`,
    );
  }
  return await r.json();
}

// ---------- Phase 17.6: magic-link + Google OAuth helpers ---------- //

export type AuthSuccess = {
  user: SessionUser;
  workspace: SessionWorkspace;
  session_token: string;
  session_expires_at?: string;
  is_new_user?: boolean;
  redirect_after?: string;
};

export async function requestMagicLink(email: string): Promise<void> {
  // Backend always returns 200 (no-leak contract); we only surface
  // genuine network or 5xx failures to the user.
  const r = await fetch(`${API_URL}/auth/magic-link/request`, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `magic-link request failed (${r.status})`);
  }
}

export async function consumeMagicLink(token: string): Promise<AuthSuccess> {
  const r = await fetch(`${API_URL}/auth/magic-link/consume`, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `magic-link consume failed (${r.status})`,
    );
  }
  return await r.json();
}

export async function startGoogleOAuth(
  redirectAfter?: string,
): Promise<{ authorization_url: string; state: string }> {
  const qs = redirectAfter
    ? `?redirect_after=${encodeURIComponent(redirectAfter)}`
    : "";
  const r = await fetch(`${API_URL}/auth/google/start${qs}`, {
    cache: "no-store",
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `google sign-in unavailable (${r.status})`,
    );
  }
  return await r.json();
}

export async function completeGoogleOAuth(
  code: string,
  state: string,
): Promise<AuthSuccess> {
  const params = new URLSearchParams({ code, state });
  const r = await fetch(
    `${API_URL}/auth/google/callback?${params.toString()}`,
    { cache: "no-store" },
  );
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = (body as { detail?: unknown })?.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      throw new Error(String((detail as { message: string }).message));
    }
    throw new Error(
      typeof detail === "string"
        ? detail
        : `google sign-in failed (${r.status})`,
    );
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
  if (r.status === 401) {
    // Phase 23.6: distinguish "session invalid → /login" from
    // "session valid but workspace context bad → /me/workspaces/pick".
    // Peek the body for the detail string the backend's auth.py
    // returns, then throw the right subclass.
    let detail: string | null = null;
    try {
      const body = await r.json();
      const raw = (body as { detail?: unknown })?.detail;
      if (typeof raw === "string") detail = raw;
    } catch {
      // body wasn't JSON; treat as generic.
    }
    if (detail && _NO_ACTIVE_DETAILS.has(detail)) {
      throw new NoActiveWorkspaceError(detail);
    }
    throw new UnauthorizedError(detail ?? "unauthorized");
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const raw = (body as { detail?: unknown })?.detail;
    // FastAPI returns `detail` as a string for HTTPException, but as an
    // array of objects for Pydantic validation failures (422). Render
    // both readably so the user never sees [object Object].
    const detailMsg =
      typeof raw === "string"
        ? raw
        : raw !== undefined
          ? JSON.stringify(raw)
          : null;
    throw new Error(detailMsg ?? `${path} returned ${r.status}`);
  }
  return r.json();
}

export async function fetchWorkspace(): Promise<SessionWorkspace> {
  return (await authedJson("/workspaces/me")) as SessionWorkspace;
}

// ---------- Feeder: weekly business digest ---------- //
// The feeder is what makes the AI Business Team proactive: it enqueues a
// bi.summarize on a weekly cadence. These power the home-page digest card.

export interface FeederDigestStatus {
  last_digest: {
    command_id: string;
    status: string;
    created_at: string;
    completed_at: string | null;
  } | null;
  latest_summary: {
    text: string | null;
    kind: string | null;
    produced_at: string;
  } | null;
  period_days: number;
}

export interface RunFeederDigestResult {
  status: string;
  command_id: string | null;
  bi_assistant_deployed: boolean;
  note: string | null;
}

export async function fetchFeederDigestStatus(): Promise<FeederDigestStatus> {
  return (await authedJson(
    "/workspaces/me/feeder/digest/status",
  )) as FeederDigestStatus;
}

export async function runFeederDigest(): Promise<RunFeederDigestResult> {
  return (await authedJson("/workspaces/me/feeder/digest", {
    method: "POST",
  })) as RunFeederDigestResult;
}

// ---------- Business onboarding ---------- //

export interface OnboardingIndustry {
  key: string;
  label: string;
}

export interface OnboardingGoal {
  key: string;
  label: string;
  assistant: string;
  connector: string | null;
  // True for goals that need a free-text target (the website goal needs a
  // site URL); the wizard shows a URL input when such a goal is checked.
  needs_url: boolean;
}

export interface OnboardingCatalog {
  industries: OnboardingIndustry[];
  goals: OnboardingGoal[];
  recommendations: Record<string, string[]>;
}

export interface OnboardingProfile {
  industry: string | null;
  goals: string[];
  completed_at: string;
}

export interface OnboardingPlan {
  industry: string | null;
  goals: string[];
  assistants: string[];
  feeders: string[];
  connectors_needed: { type: string; label: string }[];
}

export async function fetchOnboarding(): Promise<{
  catalog: OnboardingCatalog;
  profile: OnboardingProfile | null;
}> {
  return (await authedJson("/workspaces/me/onboarding")) as {
    catalog: OnboardingCatalog;
    profile: OnboardingProfile | null;
  };
}

export async function submitOnboarding(
  industry: string | null,
  goals: string[],
  websiteUrl?: string | null,
): Promise<{ status: string; plan: OnboardingPlan; profile: OnboardingProfile }> {
  return (await authedJson("/workspaces/me/onboarding", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ industry, goals, website_url: websiteUrl ?? null }),
  })) as { status: string; plan: OnboardingPlan; profile: OnboardingProfile };
}

export interface DeployTeamResult {
  deployed: string[];
  already_running: string[];
  needs_anthropic_key: boolean;
}

export async function deployTeam(): Promise<DeployTeamResult> {
  return (await authedJson("/workspaces/me/team/deploy", {
    method: "POST",
  })) as DeployTeamResult;
}

export interface TeamAssistantStatus {
  name: string;
  display_name: string;
  role: string | null;
  is_custom_name: boolean;
  status: string | null;
  running: boolean;
  deployed: boolean;
  is_llm: boolean;
}

export interface TeamStatusResult {
  assistants: TeamAssistantStatus[];
  needs_anthropic_key: boolean;
}

export async function fetchTeamStatus(): Promise<TeamStatusResult> {
  return (await authedJson("/workspaces/me/team/status")) as TeamStatusResult;
}

export async function renameAssistant(
  agentName: string,
  displayName: string,
): Promise<void> {
  await authedJson(`/workspaces/me/assistants/${agentName}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
}

// ---------- Ask your business team (chat-first insights) ---------- //

export interface AskAssistant {
  name: string;
  role: string | null;
}

export interface AskResult {
  command_id: string;
  bi_assistant_deployed: boolean;
  assistant: AskAssistant;
}

export interface AskAnswer {
  status: "pending" | "answered" | "failed";
  answer?: string;
  error?: string;
  question: string;
  assistant?: AskAssistant;
}

export async function askBusinessTeam(question: string): Promise<AskResult> {
  return (await authedJson("/workspaces/me/ask", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question }),
  })) as AskResult;
}

export async function fetchAnswer(commandId: string): Promise<AskAnswer> {
  return (await authedJson(`/workspaces/me/ask/${commandId}`)) as AskAnswer;
}

export interface AskHistoryItem {
  command_id: string;
  question: string;
  asked_at: string | null;
  status: "pending" | "answered" | "failed";
  answer?: string;
  error?: string;
}

export async function fetchAskHistory(
  limit = 10,
): Promise<{ asks: AskHistoryItem[]; assistant: AskAssistant }> {
  return (await authedJson(`/workspaces/me/ask?limit=${limit}`)) as {
    asks: AskHistoryItem[];
    assistant: AskAssistant;
  };
}

// ---------- Proactive feed ---------- //

export interface FeedItem {
  id: number;
  assistant: string;
  assistant_name: string;
  assistant_role: string | null;
  assistant_label: string;
  kind: string;
  title: string;
  detail: string | null;
  severity: "alert" | "info";
  timestamp: string;
}

export async function fetchFeed(limit = 50): Promise<FeedItem[]> {
  const body = (await authedJson(
    `/workspaces/me/feed?limit=${limit}`,
  )) as { items: FeedItem[] };
  return body.items;
}

export interface FeederSetting {
  kind: string;
  name: string;
  description: string;
  enabled: boolean;
  config: Record<string, unknown>;
  targetable: boolean;
  // True when the feeder's target is a free-text URL the owner types (the
  // website feeder), rather than a connector-backed picker.
  url_target: boolean;
}

export interface FeederTarget {
  account_id: string;
  location_id: string;
  location_title: string | null;
  account_name: string | null;
  label: string;
}

export interface FeederTargetsResult {
  targets: FeederTarget[];
  available: boolean;
  reason: string | null;
}

export async function fetchFeeders(): Promise<FeederSetting[]> {
  const body = (await authedJson("/workspaces/me/feeders")) as {
    feeders: FeederSetting[];
  };
  return body.feeders;
}

export async function setFeederEnabled(
  kind: string,
  enabled: boolean,
): Promise<FeederSetting[]> {
  const body = (await authedJson(`/workspaces/me/feeders/${kind}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ enabled }),
  })) as { feeders: FeederSetting[] };
  return body.feeders;
}

export async function setFeederConfig(
  kind: string,
  config: Record<string, unknown>,
): Promise<FeederSetting[]> {
  const body = (await authedJson(`/workspaces/me/feeders/${kind}/config`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ config }),
  })) as { feeders: FeederSetting[] };
  return body.feeders;
}

export async function fetchFeederTargets(
  kind: string,
): Promise<FeederTargetsResult> {
  return (await authedJson(
    `/workspaces/me/feeders/${kind}/targets`,
  )) as FeederTargetsResult;
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

// ----- Validators (Phase 7+) -----

export type ValidatorMode = "advisory" | "blocking";

export type ValidatorConfig = {
  event_kind: string;
  validator_name: string;
  config: Record<string, unknown>;
  mode: ValidatorMode;
  created_at: string;
  updated_at: string;
};

export async function fetchValidators(): Promise<ValidatorConfig[]> {
  const body = (await authedJson("/workspaces/me/validators")) as {
    validators: ValidatorConfig[];
  };
  return body.validators;
}

export async function putValidator(
  eventKind: string,
  validatorName: string,
  config: Record<string, unknown>,
  mode: ValidatorMode,
): Promise<ValidatorConfig> {
  return (await authedJson(
    `/workspaces/me/validators/${encodeURIComponent(eventKind)}/${encodeURIComponent(validatorName)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ config, mode }),
    },
  )) as ValidatorConfig;
}

export async function deleteValidator(
  eventKind: string,
  validatorName: string,
): Promise<void> {
  await authedJson(
    `/workspaces/me/validators/${encodeURIComponent(eventKind)}/${encodeURIComponent(validatorName)}`,
    { method: "DELETE" },
  );
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

export type AgentProvider =
  | "openai"
  | "anthropic"
  | "google"
  | "groq"
  | "xai"
  | "cohere";

// Mirror of backend's SUPPORTED_PROVIDERS — keep in sync.
export const AGENT_PROVIDERS: AgentProvider[] = [
  "openai",
  "anthropic",
  "google",
  "groq",
  "xai",
  "cohere",
];

// Phase 16.1: trust-zone sensitivity ladder.
export type SensitivityLevel = "public" | "internal" | "sensitive" | "pii";

export const SENSITIVITY_LEVELS: readonly SensitivityLevel[] = [
  "public",
  "internal",
  "sensitive",
  "pii",
];

export type Agent = {
  name: string;
  // Phase 35: customer-facing constellation identity. display_name is the
  // star name (or the owner's rename); assistant_role is the plain-English
  // business role (null for non-persona agents). `name` stays the id.
  display_name: string;
  assistant_role: string | null;
  is_custom_name: boolean;
  daily_cost_cap_usd: number | null;
  system_prompt: string | null;
  // Phase 12.1: per-agent provider + model pin. null = inherit from
  // whatever the SDK reports on the latest llm_call_completed.
  provider: AgentProvider | null;
  model: string | null;
  // Per-agent tick interval (seconds) for cron-style bots. null = bot
  // uses its env default. Reactive bots ignore this value.
  tick_interval_s: number | null;
  // Short "what this bot does" description shown on the /agents roster.
  // Auto-populated from the LLM rationale when the bot is generated
  // via /agents/generate; hand-deployed bots start null.
  description: string | null;
  // Phase 16.1: trust-zone sensitivity. Drives the dashboard chip
  // color + the SDK's auto-redaction default (Phase 16.5).
  sensitivity_level: SensitivityLevel;
  // Phase 16.2: per-agent capability allow-list. Empty list = default-
  // deny; the SDK (Phase 16.3) refuses any gated op not on the list.
  capabilities: string[];
  // Phase 16.4: opt-in for cross-zone dispatch. False = same-zone-only
  // dispatches; True allows the source agent to target a different zone
  // (auto-approval rules still apply on top).
  dispatches_cross_zone: boolean;
  created_at: string;
  updated_at: string;
};

export async function fetchAgent(name: string): Promise<Agent> {
  return (await authedJson(
    `/agents/${encodeURIComponent(name)}`,
  )) as Agent;
}


// ---------- Phase 14.4: quality signal ---------- //
//
// Reads against `run_evaluations` populated by the Phase 14.3 eval
// runner. The /agents Quality column polls `fetchWorkspaceQuality`;
// /agents/{name}'s Quality section polls `fetchAgentQuality` for
// the recent-bads reasons it renders inline.

export type Verdict = "good" | "borderline" | "bad";

export type VerdictCounts = {
  good: number;
  borderline: number;
  bad: number;
};

export type QualityTrend = {
  delta_pp: number;
  direction: "up" | "down" | "flat" | "unknown";
};

export type RecentBad = {
  run_id: string;
  agent_name: string;
  reasons: string[];
  confidence: number;
  judge_model: string;
  created_at: string;
  run_started_at: string | null;
};

export type AgentQualitySummary = {
  agent_name: string;
  verdict_counts: VerdictCounts;
  total_evaluations: number;
  trend: QualityTrend;
};

export type AgentQuality = AgentQualitySummary & {
  days: number;
  recent_bads: RecentBad[];
};

export type WorkspaceQuality = {
  days: number;
  verdict_counts: VerdictCounts;
  total_evaluations: number;
  per_agent: AgentQualitySummary[];
  recent_bads: RecentBad[];
};

export async function fetchWorkspaceQuality(
  days = 7,
): Promise<WorkspaceQuality> {
  return (await authedJson(
    `/workspaces/me/quality?days=${days}`,
  )) as WorkspaceQuality;
}

export async function fetchAgentQuality(
  name: string,
  days = 7,
): Promise<AgentQuality> {
  return (await authedJson(
    `/workspaces/me/agents/${encodeURIComponent(name)}/quality?days=${days}`,
  )) as AgentQuality;
}

export async function patchAgent(
  name: string,
  patch: Partial<{
    daily_cost_cap_usd: number | null;
    system_prompt: string | null;
    provider: AgentProvider | null;
    model: string | null;
    tick_interval_s: number | null;
    description: string | null;
    // Phase 16: trust-zone knobs editable through the existing patch endpoint.
    sensitivity_level: SensitivityLevel;
    dispatches_cross_zone: boolean;
  }>,
): Promise<Agent> {
  return (await authedJson(`/agents/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  })) as Agent;
}

export async function deleteAgent(name: string): Promise<void> {
  await authedJson(`/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}


// Phase 16.2: replace an agent's capability allow-list.
// Returns 422 with {"detail": {"problems": [...]}} on validation
// errors so the caller can render per-entry messages.
export async function patchAgentCapabilities(
  name: string,
  capabilities: string[],
): Promise<Agent> {
  return (await authedJson(
    `/agents/${encodeURIComponent(name)}/capabilities`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ capabilities }),
    },
  )) as Agent;
}


// Phase 16.7: trust-zone presets the team-from-README picker offers.

export type ZonePresetRoleConfig = {
  sensitivity_level: SensitivityLevel;
  capabilities: string[];
  dispatches_cross_zone: boolean;
};

export type ZonePreset = {
  name: string;
  label: string;
  summary: string;
  tradeoff: string;
  by_role: Record<string, ZonePresetRoleConfig>;
  // P16.x: hint-aware mapping. Empty for non-hint-aware presets
  // (Open / Standard); populated for Compliance with one config per
  // sensitivity level. When the planner emits a sensitivity_hint per
  // bot, the deploy code prefers by_hint[hint] over by_role[role].
  by_hint: Record<string, ZonePresetRoleConfig>;
  is_default: boolean;
};

export async function fetchZonePresets(): Promise<ZonePreset[]> {
  const body = (await authedJson(
    "/workspaces/me/zone-presets",
  )) as { presets: ZonePreset[] };
  return body.presets;
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

// Phase 12B.1: describe-a-bot generator. Calls Claude server-side
// with the curated SDK prompt + the workspace's existing constellation.
// Returns the generated bot.py + requirements.txt for the user to
// review/edit before deploying.
export type AgentGenerateInput = {
  description: string;
  target_agents?: string[];
  name_hint?: string;
  // Phase 12B.3 iteration loop. Set all three together to refine a
  // prior generation rather than start over.
  tweak_request?: string;
  previous_bot_py?: string;
  previous_requirements_txt?: string;
};

export type AgentGenerateOutput = {
  agent_name_suggestion: string;
  rationale: string;
  bot_py: string;
  requirements_txt: string;
  model_used: string;
  tokens_in: number | null;
  tokens_out: number | null;
};

export async function generateAgent(
  input: AgentGenerateInput,
): Promise<AgentGenerateOutput> {
  // Phase 12C.6: the endpoint moved off the request path. POST returns
  // 202 + {job_id}; the result lands on `result_payload` of the job row
  // once the in-process runner finishes the Anthropic call. Callers'
  // signature is unchanged; the polling is hidden here.
  const kicked = (await authedJson("/workspaces/me/agents/generate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as { job_id: string; status: string };
  return (await pollGenerationJob(
    kicked.job_id,
  )) as AgentGenerateOutput;
}

// Shared poll loop for /workspaces/me/generation-jobs/{id}. Resolves
// with the job's `result_payload` (typed by the caller) on
// `status='success'`, throws the captured `error` text on
// `status='failed'`. Backoff 1s → 2s → 4s → 5s cap; total wall-clock
// cap ~5 min so the UI doesn't hang forever if the runner wedges.
//
// Why polling and not SSE / websockets: one Railway service, no
// long-lived connections in front of us, and the result rows are small
// enough that O(1 poll per 5s) is fine. Revisit if we ever burn the
// API gateway with poll volume.
async function pollGenerationJob(jobId: string): Promise<unknown> {
  const start = Date.now();
  const capMs = 5 * 60_000;
  let delayMs = 1_000;
  // Initial micro-wait so a fast handler doesn't take a full second to
  // surface; the runner's idle sleep is 500ms.
  await new Promise((r) => setTimeout(r, 250));
  while (true) {
    const row = (await authedJson(
      `/workspaces/me/generation-jobs/${encodeURIComponent(jobId)}`,
    )) as {
      status: "pending" | "running" | "success" | "failed";
      result_payload: unknown;
      error: string | null;
    };
    if (row.status === "success") return row.result_payload;
    if (row.status === "failed") {
      throw new Error(row.error || "generation job failed");
    }
    if (Date.now() - start > capMs) {
      throw new Error(
        `generation job ${jobId} still ${row.status} after ${Math.round(capMs / 1000)}s; gave up polling`,
      );
    }
    await new Promise((r) => setTimeout(r, delayMs));
    delayMs = Math.min(delayMs * 2, 5_000);
  }
}

// Browser-native deploy. Same multipart shape the CLI uses: agent_name
// as a form field and `bundle` as the .zip file. We bypass authedJson
// because that helper sets content-type=application/json; multipart
// uploads need fetch's auto-boundary instead.
export async function uploadDeploymentBundle(
  agentName: string,
  zipFile: File,
  onProgress?: (loaded: number, total: number) => void,
): Promise<Deployment> {
  const form = new FormData();
  form.append("agent_name", agentName);
  form.append("bundle", zipFile, zipFile.name || "bundle.zip");

  // XMLHttpRequest gives us upload progress events; fetch() can't.
  return await new Promise<Deployment>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_URL}/workspaces/me/deployments`);
    const headers = authHeaders();
    Object.entries(headers).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
    }
    xhr.onerror = () => reject(new Error("network error during upload"));
    xhr.onload = () => {
      if (xhr.status === 401) {
        reject(new UnauthorizedError());
        return;
      }
      if (xhr.status >= 400) {
        let msg = `${xhr.status}`;
        try {
          const body = JSON.parse(xhr.responseText);
          if (typeof body?.detail === "string") msg = body.detail;
          else if (body?.detail !== undefined) msg = JSON.stringify(body.detail);
        } catch {
          /* ignore */
        }
        reject(new Error(msg));
        return;
      }
      try {
        resolve(JSON.parse(xhr.responseText) as Deployment);
      } catch (e) {
        reject(new Error(`invalid response: ${e}`));
      }
    };
    xhr.send(form);
  });
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


// Phase 12D.2: Polaris narrates the same insight stream rendered on
// /cost/insights. `CostInsight` / `CostInsightApply` are defined later
// in this file (the /cost/insights page also imports them); the
// payload shape here mirrors the event Polaris emits on tick.
export type PolarisCostAnalysis = {
  event_id: number;
  timestamp: string;
  payload: {
    insights: CostInsight[];
    generated_at: string;
    window_days: number;
  };
};

export async function fetchLatestPolarisCostAnalysis(
  agentName: string,
): Promise<PolarisCostAnalysis | null> {
  const r = await fetch(
    `${API_URL}/agents/${encodeURIComponent(agentName)}/latest-cost-analysis`,
    { cache: "no-store", headers: authHeaders() },
  );
  if (r.status === 401) throw new UnauthorizedError();
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`/latest-cost-analysis returned ${r.status}`);
  return (await r.json()) as PolarisCostAnalysis;
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

// ---------- Phase 10B: GitHub OAuth + multi-repo ---------- //

export type GithubConnection = {
  id: string;
  auth_kind: "oauth" | "pat";
  github_login: string | null;
  created_at: string;
  updated_at: string;
};

export type GithubRepo = {
  id: string;
  repo_owner: string;
  repo_name: string;
  branch: string;
  is_active: boolean;
  created_at: string;
  // Present only on the POST response, revealed exactly once.
  webhook_secret?: string;
};

export type GithubConnectionState = {
  connection: GithubConnection | null;
  repos: GithubRepo[];
};

export async function startGithubOAuth(
  redirectAfter?: string,
): Promise<{ authorization_url: string; state: string }> {
  const q = redirectAfter ? `?redirect_after=${encodeURIComponent(redirectAfter)}` : "";
  return (await authedJson(`/workspaces/me/github/oauth/start${q}`)) as {
    authorization_url: string;
    state: string;
  };
}

export async function fetchGithubConnection(): Promise<GithubConnectionState> {
  return (await authedJson("/workspaces/me/github/connection")) as GithubConnectionState;
}

// ---------- SEO assistant (Spica) ---------- //

export interface SeoPage {
  title: string;
  meta_description: string;
  slug: string;
  h1: string;
  body_html: string;
}

export interface SeoDraft {
  id: string;
  keyword: string | null;
  created_at: string | null;
  page: SeoPage;
}

export async function fetchSeoDrafts(): Promise<SeoDraft[]> {
  const body = (await authedJson("/workspaces/me/seo/drafts")) as {
    drafts: SeoDraft[];
  };
  return body.drafts;
}

export async function publishPage(input: {
  repo_id: string;
  path: string;
  content: string;
  title: string;
  body?: string;
}): Promise<{ pr_url: string; pr_number: number; branch: string }> {
  return (await authedJson("/workspaces/me/github/publish-page", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as { pr_url: string; pr_number: number; branch: string };
}

export async function addGithubRepo(input: {
  repo_owner: string;
  repo_name: string;
  branch: string;
}): Promise<GithubRepo> {
  return (await authedJson("/workspaces/me/github/repos", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as GithubRepo;
}

export async function removeGithubRepo(repoId: string): Promise<void> {
  await authedJson(`/workspaces/me/github/repos/${encodeURIComponent(repoId)}`, {
    method: "DELETE",
  });
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


// ---------- Team planner (Phase 12C.1) ---------- //
//
// `POST /workspaces/me/teams/plan` takes a project description and
// returns 3-7 proposed bots wired into a constellation. The dashboard's
// `/agents/team-from-readme` page (12C.2) consumes this; 12C.3 will
// loop through the team and call `/agents/generate` per member.

export type TeamMemberRole = "orchestrator" | "specialist" | "messenger";

// P16.x: planner emits a per-bot trust-zone hint so the Compliance
// preset's deploy code can land each bot in the right zone without
// the operator manually overriding (the gap the Coral demo exposed).
export type SensitivityHint = "public" | "internal" | "sensitive" | "pii";

export type TeamMember = {
  name: string;
  role: TeamMemberRole;
  sensitivity_hint: SensitivityHint;
  summary: string;
  command_kinds: string[];
  dispatches_to: string[];
  needs_workspace_secrets: string[];
  // Phase 24.1: required per-bot capability allow-list emitted by the
  // planner. Empty array = operator-only (bot has no outbound powers
  // until granted). 24.3 wires this into the deploy path so the Agent
  // row's capabilities column is populated from this value instead of
  // the server default.
  capabilities: string[];
  draft_description: string;
};

// Phase 24.2: client-side mirror of backend/capabilities.py's
// KNOWN_CAPABILITIES vocabulary. Powers the MemberPanel's capability
// editor typeahead. The connector:<name> prefix family is accepted at
// the validator (isValidCapabilityFormat), even when the name isn't in
// this list — same forward-compat shape the backend uses.
export const KNOWN_CAPABILITIES: readonly string[] = [
  "internet",
  "send_command",
  "slack:respond",
  "widget:respond",
  "widget:escalate",
  // Connector-shaped capabilities the planner is likely to propose,
  // surfaced as suggestions in the typeahead. Custom connector:<name>
  // strings (e.g. connector:jyni_crm) also pass validation.
  "connector:gmail",
  "connector:google_calendar",
  "connector:google_drive",
];

const _CONNECTOR_PREFIX = "connector:";
const _MAX_CAPABILITY_LEN = 64;

export function isValidCapabilityFormat(name: string): boolean {
  if (!name || name.length > _MAX_CAPABILITY_LEN) return false;
  if (KNOWN_CAPABILITIES.includes(name)) return true;
  if (!name.startsWith(_CONNECTOR_PREFIX)) return false;
  const suffix = name.slice(_CONNECTOR_PREFIX.length);
  // Match the backend: non-empty, no leading/trailing whitespace,
  // alphanumeric + dash + underscore only.
  return (
    suffix.length > 0
    && suffix.trim() === suffix
    && /^[A-Za-z0-9_-]+$/.test(suffix)
  );
}

export type TeamPlan = {
  rationale: string;
  team: TeamMember[];
  model_used: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
};

export type TeamPlanInput = {
  readme_text?: string;
  freeform_description?: string;
  github_repo?: string;
  github_branch?: string;
};

export async function fetchTeamPlan(input: TeamPlanInput): Promise<TeamPlan> {
  // Phase 12C.6: same async-job pattern as generateAgent. POST → 202 +
  // {job_id}, then poll until terminal. Signature unchanged so
  // team-from-readme/page.tsx's "thinking..." state keeps working.
  const kicked = (await authedJson("/workspaces/me/teams/plan", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as { job_id: string; status: string };
  return (await pollGenerationJob(kicked.job_id)) as TeamPlan;
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
  // Phase 35: customer-facing constellation name + business role.
  display_name: string;
  assistant_role: string | null;
  role: "orchestrator" | "executor" | "notifier" | "specialist";
  model: string | null;
  status: "active" | "stale" | "stopped";
  runs_24h: number;
  cost_24h_usd: number;
  last_event_at: string | null;
  last_heartbeat_at: string | null;
  // Phase 16.6: drives node color on the constellation map.
  sensitivity_level: SensitivityLevel;
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

// Phase 12D.1: cost-intelligence insights. Each entry is one
// recommendation or audit datapoint with a homogeneous shape so the
// rendering can be a generic map. `apply` is non-null when the
// dashboard can offer a one-click fix.
export type CostInsightApply = {
  href: string;
  label: string;
  patch?: Record<string, unknown>;
};

export type CostInsight = {
  kind: string;
  headline: string;
  detail: Record<string, unknown>;
  apply: CostInsightApply | null;
};

export async function fetchCostInsights(): Promise<CostInsight[]> {
  const body = (await authedJson("/workspaces/me/cost/insights")) as {
    insights: CostInsight[];
  };
  return body.insights;
}

// ---------- Status pulse (Phase 11B.2) ---------- //

export type PulseIssues = {
  pending_approvals: number;
  failed_validations: number;
  budget_warnings: number;
  stale_agents: number;
};

export type WorkspacePulse = {
  status: "calm" | "attention";
  issues_count: number;
  issues: PulseIssues;
  workspace_name: string;
  agent_count: number;
  last_polaris_tick_at: string | null;
  last_event_at: string | null;
  as_of: string;
};

export async function fetchWorkspacePulse(): Promise<WorkspacePulse> {
  return (await authedJson("/workspaces/me/pulse")) as WorkspacePulse;
}

// ---------- Phase 11.6: dispatch chain views ---------- //

export type DispatchChainStatus =
  | "pending"
  | "pending_approval"
  | "running"
  | "done"
  | "failed"
  | "expired"
  | "rejected";

export type DispatchChainSummary = {
  chain_id: string;
  started_at: string;
  last_activity_at: string;
  command_count: number;
  max_depth: number;
  root_agent: string;
  root_kind: string;
  root_source_agent: string | null;
  status: DispatchChainStatus;
  pending_approval_count: number;
};

export type DispatchCommand = {
  id: string;
  agent_name: string;
  kind: string;
  payload: Record<string, unknown>;
  status: string;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  claimed_at: string | null;
  completed_at: string | null;
  expires_at: string;
  source_agent: string | null;
  dispatch_chain_id: string;
  dispatch_depth: number;
  approval_state: string;
  approved_by_user_id: string | null;
  approved_at: string | null;
};

export type DispatchEvent = {
  id: string;
  run_id: string;
  agent_name: string;
  kind: string;
  payload: Record<string, unknown>;
  timestamp: string;
  command_id: string | null;
};

export type DispatchChainDetail = {
  chain_id: string;
  commands: DispatchCommand[];
  events: DispatchEvent[];
  status: DispatchChainStatus;
};

export async function fetchDispatchChains(): Promise<DispatchChainSummary[]> {
  const body = (await authedJson("/workspaces/me/dispatch")) as {
    chains: DispatchChainSummary[];
  };
  return body.chains;
}

export async function fetchDispatchChain(
  chainId: string,
): Promise<DispatchChainDetail> {
  return (await authedJson(
    `/workspaces/me/dispatch/${encodeURIComponent(chainId)}`,
  )) as DispatchChainDetail;
}

export async function approveCommand(commandId: string): Promise<DispatchCommand> {
  // Backend's CommandApprovalIn body is required (with optional `reason`),
  // so we have to send a JSON object even when there's no reason.
  return (await authedJson(`/commands/${encodeURIComponent(commandId)}/approve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  })) as DispatchCommand;
}

export async function rejectCommand(commandId: string): Promise<DispatchCommand> {
  return (await authedJson(`/commands/${encodeURIComponent(commandId)}/reject`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  })) as DispatchCommand;
}

// ---------- Phase 11.2 / 11.6: auto-approval rule editor ---------- //

export type AutoApprovalRule = {
  source_agent: string;
  target_agent: string;
  command_kind: string;
  mode: "auto_approve" | "require_human";
  created_at: string;
  updated_at: string;
};

export async function fetchAutoApprovalRules(): Promise<AutoApprovalRule[]> {
  const body = (await authedJson("/workspaces/me/auto-approval-rules")) as {
    rules: AutoApprovalRule[];
  };
  return body.rules;
}

export async function upsertAutoApprovalRule(
  rule: Omit<AutoApprovalRule, "created_at" | "updated_at">,
): Promise<AutoApprovalRule> {
  return (await authedJson("/workspaces/me/auto-approval-rules", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(rule),
  })) as AutoApprovalRule;
}

export async function deleteAutoApprovalRule(rule: {
  source_agent: string;
  target_agent: string;
  command_kind: string;
}): Promise<void> {
  // The backend DELETE takes query params, not a body — match its shape.
  const qs = new URLSearchParams({
    source_agent: rule.source_agent,
    target_agent: rule.target_agent,
    command_kind: rule.command_kind,
  });
  await authedJson(`/workspaces/me/auto-approval-rules?${qs.toString()}`, {
    method: "DELETE",
  });
}


// ---------- Phase 19.7: Slack chat-surface helpers ---------- //

export type SlackWorkspaceSummary = {
  slack_team_id: string;
  team_name: string;
  bot_user_id: string;
  installed_at: string;
  installed_by_user_id: string | null;
  revoked_at: string | null;
};

export type SlackChannelSummary = {
  slack_team_id: string;
  channel_id: string;
  channel_name: string;
  sensitivity_level: SensitivityLevel;
  opted_in: boolean;
  created_at: string;
  updated_at: string;
};

export async function startSlackOAuth(): Promise<{
  authorization_url: string;
  state: string;
}> {
  return (await authedJson("/slack/oauth/start")) as {
    authorization_url: string;
    state: string;
  };
}

export async function fetchSlackWorkspaces(
  options?: { includeRevoked?: boolean },
): Promise<SlackWorkspaceSummary[]> {
  const qs = options?.includeRevoked ? "?include_revoked=true" : "";
  const body = (await authedJson(`/workspaces/me/slack/workspaces${qs}`)) as {
    workspaces: SlackWorkspaceSummary[];
  };
  return body.workspaces;
}

export async function fetchSlackChannels(
  options?: { slackTeamId?: string },
): Promise<SlackChannelSummary[]> {
  const qs = options?.slackTeamId
    ? `?slack_team_id=${encodeURIComponent(options.slackTeamId)}`
    : "";
  const body = (await authedJson(`/workspaces/me/slack/channels${qs}`)) as {
    channels: SlackChannelSummary[];
  };
  return body.channels;
}

export async function patchSlackChannel(
  slackTeamId: string,
  channelId: string,
  patch: { sensitivity_level?: SensitivityLevel; opted_in?: boolean },
): Promise<SlackChannelSummary> {
  return (await authedJson(
    `/workspaces/me/slack/channels/${encodeURIComponent(slackTeamId)}/${encodeURIComponent(channelId)}`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    },
  )) as SlackChannelSummary;
}

export async function revokeSlackWorkspace(
  slackTeamId: string,
): Promise<SlackWorkspaceSummary> {
  return (await authedJson(
    `/workspaces/me/slack/workspaces/${encodeURIComponent(slackTeamId)}`,
    { method: "DELETE" },
  )) as SlackWorkspaceSummary;
}


// ---------- Phase 20.8: connector helpers ---------- //

export type ConnectorInstallSummary = {
  id: string;
  external_account_email: string | null;
  scopes: string[];
  installed_at: string;
  installed_by_user_id: string | null;
  revoked_at: string | null;
};

export type ConnectorSummary = {
  type: string;
  display_label: string;
  oauth_provider: string;
  default_scopes: string[];
  declared_zones: SensitivityLevel[];
  summary: string;
  install: ConnectorInstallSummary | null;
};

export async function fetchConnectors(): Promise<ConnectorSummary[]> {
  const body = (await authedJson("/workspaces/me/connectors")) as {
    connectors: ConnectorSummary[];
  };
  return body.connectors;
}

export async function startConnectorOAuth(
  connectorType: string,
  options?: { redirectAfter?: string },
): Promise<{ authorization_url: string; state: string }> {
  const params = new URLSearchParams({ type: connectorType });
  if (options?.redirectAfter) params.set("redirect_after", options.redirectAfter);
  return (await authedJson(
    `/connectors/google/start?${params.toString()}`,
  )) as { authorization_url: string; state: string };
}

export async function disconnectConnector(
  connectorType: string,
): Promise<{ status: string; connector_type: string; revoked_at: string }> {
  return (await authedJson(
    `/workspaces/me/connectors/${encodeURIComponent(connectorType)}`,
    { method: "DELETE" },
  )) as { status: string; connector_type: string; revoked_at: string };
}


// ---------- Phase 21.7: widget settings helpers ---------- //

export type WidgetAvailableAgent = {
  name: string;
  description: string | null;
  sensitivity_level: SensitivityLevel;
  has_widget_capabilities: boolean;
};

export type WidgetSettings = {
  widget_public_id: string;
  customer_facing_agent_name: string | null;
  allowed_widget_origins: string[];
  available_agents: WidgetAvailableAgent[];
  // Phase 36.1: branding (null = default).
  widget_title: string | null;
  widget_accent_color: string | null;
  widget_greeting: string | null;
  // Phase 36.2: remove-branding toggle + whether the plan allows it.
  widget_hide_branding: boolean;
  can_remove_branding: boolean;
};

export async function fetchWidgetSettings(): Promise<WidgetSettings> {
  return (await authedJson(
    "/workspaces/me/widget-settings",
  )) as WidgetSettings;
}

export async function patchWidgetSettings(patch: {
  customer_facing_agent_name?: string | null;
  allowed_widget_origins?: string[];
  widget_title?: string | null;
  widget_accent_color?: string | null;
  widget_greeting?: string | null;
  widget_hide_branding?: boolean;
}): Promise<WidgetSettings> {
  return (await authedJson("/workspaces/me/widget-settings", {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  })) as WidgetSettings;
}


// ---------- Phase 21.8: operator inbox helpers ---------- //

export type InboxConversationRow = {
  id: string;
  status: "open" | "escalated" | "operator_owned" | "resolved";
  customer_facing_agent_name: string | null;
  sensitivity_level: SensitivityLevel | null;
  anon_user_id: string | null;
  started_at: string;
  last_message_at: string;
  resolved_at: string | null;
  open_escalation_count: number;
  last_message_preview: string;
  last_message_role: "user" | "bot" | "operator" | "system" | null;
};

export type InboxListResponse = {
  conversations: InboxConversationRow[];
  filter: string;
  limit: number;
  as_of: string;
};

export type InboxMessage = {
  id: number;
  role: "user" | "bot" | "operator" | "system";
  text: string;
  sent_at: string;
};

export type InboxEscalation = {
  id: string;
  reason: string;
  payload: Record<string, unknown>;
  suggested_fix: Record<string, unknown> | null;
  escalated_at: string;
  resolved_at: string | null;
};

export type InboxConversationDetail = {
  id: string;
  status: InboxConversationRow["status"];
  customer_facing_agent_name: string | null;
  sensitivity_level: SensitivityLevel | null;
  anon_user_id: string | null;
  started_at: string;
  last_message_at: string;
  resolved_at: string | null;
  messages: InboxMessage[];
  escalations: InboxEscalation[];
};

export async function fetchInbox(options?: {
  status?: string;
  since?: string;
}): Promise<InboxListResponse> {
  const params = new URLSearchParams();
  if (options?.status) params.set("status", options.status);
  if (options?.since) params.set("since", options.since);
  const qs = params.toString() ? `?${params}` : "";
  return (await authedJson(`/workspaces/me/inbox${qs}`)) as InboxListResponse;
}

export async function fetchInboxConversation(
  conversationId: string,
): Promise<InboxConversationDetail> {
  return (await authedJson(
    `/workspaces/me/inbox/${encodeURIComponent(conversationId)}`,
  )) as InboxConversationDetail;
}

export async function takeOverConversation(
  conversationId: string,
): Promise<{ ok: boolean; status: string; noop?: boolean }> {
  return (await authedJson(
    `/workspaces/me/inbox/${encodeURIComponent(conversationId)}/take-over`,
    { method: "POST" },
  )) as { ok: boolean; status: string; noop?: boolean };
}

export async function postInboxOperatorReply(
  conversationId: string,
  text: string,
): Promise<{ ok: boolean; message_id: number; conversation_id: string }> {
  return (await authedJson(
    `/workspaces/me/inbox/${encodeURIComponent(conversationId)}/messages`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    },
  )) as { ok: boolean; message_id: number; conversation_id: string };
}

export async function resolveConversation(
  conversationId: string,
): Promise<{
  ok: boolean;
  status: string;
  noop?: boolean;
  resolved_escalation_count?: number;
}> {
  return (await authedJson(
    `/workspaces/me/inbox/${encodeURIComponent(conversationId)}/resolve`,
    { method: "POST" },
  )) as {
    ok: boolean;
    status: string;
    noop?: boolean;
    resolved_escalation_count?: number;
  };
}


// ---------- Phase 21.9: incident-response helpers ---------- //

export type IncidentScanResponse = {
  clusters_found: number;
  fixes_generated: number;
  fixes_applied: number;
  conversations_touched: number;
  auto_apply_enabled?: boolean;
};

export async function scanWidgetIncidentPatterns(options?: {
  lookback_hours?: number;
  min_size?: number;
}): Promise<IncidentScanResponse> {
  const params = new URLSearchParams();
  if (options?.lookback_hours !== undefined) {
    params.set("lookback_hours", String(options.lookback_hours));
  }
  if (options?.min_size !== undefined) {
    params.set("min_size", String(options.min_size));
  }
  const qs = params.toString() ? `?${params}` : "";
  return (await authedJson(
    `/workspaces/me/widget-incident-response/scan${qs}`,
    { method: "POST" },
  )) as IncidentScanResponse;
}

export async function applyEscalationSuggestedFix(
  conversationId: string,
  escalationId: string,
): Promise<{
  ok: boolean;
  applied: boolean;
  conversations_touched: string[];
  agents_mutated: string[];
}> {
  return (await authedJson(
    `/workspaces/me/inbox/${encodeURIComponent(
      conversationId,
    )}/escalations/${encodeURIComponent(escalationId)}/apply-fix`,
    { method: "POST" },
  )) as {
    ok: boolean;
    applied: boolean;
    conversations_touched: string[];
    agents_mutated: string[];
  };
}

export async function dismissEscalationSuggestedFix(
  conversationId: string,
  escalationId: string,
): Promise<{ ok: boolean; dismissed: boolean; noop?: boolean }> {
  return (await authedJson(
    `/workspaces/me/inbox/${encodeURIComponent(
      conversationId,
    )}/escalations/${encodeURIComponent(escalationId)}/dismiss-fix`,
    { method: "POST" },
  )) as { ok: boolean; dismissed: boolean; noop?: boolean };
}

// ---------- Phase 22.7: trigger helpers ---------- //

export type TriggerKind = "cron" | "webhook";

export type Trigger = {
  id: string;
  workspace_id: string;
  agent_name: string;
  kind: TriggerKind;
  schedule: string | null;
  name: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_run_status: string | null;
  created_at: string;
  updated_at: string;
};

// Webhook-kind create responses include the plaintext token exactly
// once (the row only stores its sha256 hash). Operators must capture
// it from the dashboard modal; there's no recovery path.
export type TriggerWithToken = Trigger & {
  webhook_token?: string;
};

export type TriggerCreateBody =
  | { kind: "cron"; name: string; schedule: string }
  | { kind: "cron"; name: string; preset: string }
  | { kind: "webhook"; name: string };

export async function listAgentTriggers(
  agentName: string,
): Promise<Trigger[]> {
  const body = (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/triggers`,
  )) as { triggers: Trigger[] };
  return body.triggers;
}

export async function createAgentTrigger(
  agentName: string,
  body: TriggerCreateBody,
): Promise<TriggerWithToken> {
  return (await authedJson(
    `/agents/${encodeURIComponent(agentName)}/triggers`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    },
  )) as TriggerWithToken;
}

export async function patchTrigger(
  triggerId: string,
  patch: { enabled?: boolean; name?: string; schedule?: string },
): Promise<Trigger> {
  return (await authedJson(`/triggers/${encodeURIComponent(triggerId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  })) as Trigger;
}

export async function deleteTrigger(triggerId: string): Promise<void> {
  await authedJson(`/triggers/${encodeURIComponent(triggerId)}`, {
    method: "DELETE",
  });
}

export async function previewSchedule(
  schedule: string,
  count = 3,
): Promise<string[]> {
  const body = (await authedJson(`/triggers/preview-schedule`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ schedule, count }),
  })) as { next_runs: string[] };
  return body.next_runs;
}

// ---------- Phase 23.3-23.5: multi-workspace helpers ---------- //

export type WorkspaceMembership = {
  id: string;
  name: string;
  role: "owner" | "member";
  joined_at: string;
  is_active: boolean;
  plan_tier: string;
  created_at: string;
  // Phase 26.1: operator-claimed consumer-chat URL handle. NULL
  // until the operator claims one via POST /workspaces/me/vendor-slug.
  vendor_slug: string | null;
};

export async function listMyWorkspaces(): Promise<WorkspaceMembership[]> {
  const body = (await authedJson(`/me/workspaces`)) as {
    workspaces: WorkspaceMembership[];
  };
  return body.workspaces;
}

export async function createMyWorkspace(
  name: string,
): Promise<WorkspaceMembership> {
  return (await authedJson(`/me/workspaces`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  })) as WorkspaceMembership;
}

export async function switchMyWorkspace(
  workspaceId: string,
): Promise<WorkspaceMembership> {
  return (await authedJson(
    `/me/workspaces/${encodeURIComponent(workspaceId)}/switch`,
    { method: "POST" },
  )) as WorkspaceMembership;
}

export async function patchMyWorkspace(
  workspaceId: string,
  patch: { name?: string },
): Promise<WorkspaceMembership> {
  return (await authedJson(
    `/me/workspaces/${encodeURIComponent(workspaceId)}`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    },
  )) as WorkspaceMembership;
}

export async function deleteMyWorkspace(
  workspaceId: string,
): Promise<{ deleted: boolean; workspace_id: string; switched_to: string | null }> {
  return (await authedJson(
    `/me/workspaces/${encodeURIComponent(workspaceId)}`,
    { method: "DELETE" },
  )) as { deleted: boolean; workspace_id: string; switched_to: string | null };
}

// Phase 26.1: claim the consumer-chat URL handle for the active
// workspace. Backend returns the full serialized workspace; callers
// typically just need to refetch /me/workspaces afterward to pick
// up the new vendor_slug in the membership row.
export async function claimVendorSlug(
  slug: string,
): Promise<{ vendor_slug: string | null }> {
  const body = (await authedJson(`/workspaces/me/vendor-slug`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ slug }),
  })) as { vendor_slug: string | null };
  return body;
}

// ----- Phase 25.2 + 26.2: end-user consumer surface -----

export type EndUser = {
  id: string;
  email: string;
  display_name: string | null;
  email_verified: boolean;
  auth_provider: string;
  created_at: string;
};

export type EndUserVendor = {
  id: string;
  name: string;
  vendor_slug: string | null;
  widget_public_id: string | null;
  customer_facing_agent_name: string | null;
  // Phase 27.5: the per-link settings are included when the source
  // is /me/end-user/vendors/{slug} (so the settings page can render
  // the form pre-populated in one fetch). Other surfaces that
  // serialize a vendor projection may omit these fields.
  notification_pref?: string;
  display_name_override?: string | null;
};

export type EndUserMeResponse = {
  end_user: EndUser;
  linked_vendors: EndUserVendor[];
  // Phase 28.5: VAPID public key + push subscription state, shipped
  // alongside identity so /c can render the EnablePushPrompt without
  // a second fetch. `push_vapid_public_key` is null when the backend
  // isn't configured for live push (capture mode / local dev); the
  // prompt hides itself in that case.
  push_vapid_public_key?: string | null;
  has_active_push_subscription?: boolean;
};

export type EndUserVendorConversation = {
  id: string;
  status: string;
  customer_facing_agent_name: string | null;
  started_at: string;
  last_message_at: string;
  resolved_at: string | null;
};

export type WidgetMessage = {
  id: number;
  role: "user" | "bot" | "operator" | "system";
  text: string;
  sent_at: string;
};

export type WidgetThread = {
  conversation_id: string;
  status: string;
  messages: WidgetMessage[];
};

export type EndUserAuthSuccess = {
  end_user: EndUser;
  session_token: string;
  session_expires_at: string;
  is_new_end_user: boolean;
  vendor_invite_code: string | null;
  linked_vendors: EndUserVendor[];
};

// Distinct from the operator UnauthorizedError so a /c page can
// react differently (e.g., kick to /c/auth/magic-link instead of
// /login). The handleAuthError helper still routes to /login by
// default; consumer-surface pages handle this class themselves.
export class EndUserUnauthorizedError extends Error {
  constructor(message = "end-user unauthorized") {
    super(message);
    this.name = "EndUserUnauthorizedError";
  }
}

// Phase 26.3: top-level helper for /c routes that need end-user auth.
//
// Attaches the bearer from the end-user session helpers, clears on
// 401 (so stale tokens don't loop forever), and translates the
// backend's structured error detail into a thrown Error.
//
// Exported (not file-local) per spec so /c page components can call
// it directly when their needs don't match the typed fetcher
// functions below.
export async function endUserAuthedJson(
  path: string,
  init?: RequestInit,
): Promise<unknown> {
  const token = getEndUserSessionToken();
  const headers = new Headers(init?.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const r = await fetch(`${API_URL}${path}`, {
    ...(init ?? {}),
    headers,
    cache: "no-store",
  });
  if (r.status === 401) {
    // Stale / missing token. Clear so the next page load doesn't
    // re-attempt with the same dead bearer.
    clearEndUserSession();
    throw new EndUserUnauthorizedError();
  }
  if (!r.ok) {
    const body = (await r.json().catch(() => ({}))) as {
      detail?: unknown;
    };
    const detail = body.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      throw new Error(String((detail as { message: string }).message));
    }
    throw new Error(
      typeof detail === "string" ? detail : `request failed (${r.status})`,
    );
  }
  return await r.json();
}

export async function requestEndUserMagicLink(
  email: string,
  opts?: { vendor_invite_code?: string },
): Promise<void> {
  const r = await fetch(`${API_URL}/auth/end-user/magic-link/request`, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      email,
      ...(opts?.vendor_invite_code
        ? { vendor_invite_code: opts.vendor_invite_code }
        : {}),
    }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `magic-link request failed (${r.status})`);
  }
}

export async function consumeEndUserMagicLink(
  token: string,
  opts?: { vendor_invite_code?: string },
): Promise<EndUserAuthSuccess> {
  const r = await fetch(`${API_URL}/auth/end-user/magic-link/consume`, {
    method: "POST",
    cache: "no-store",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      token,
      ...(opts?.vendor_invite_code
        ? { vendor_invite_code: opts.vendor_invite_code }
        : {}),
    }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `magic-link consume failed (${r.status})`,
    );
  }
  return (await r.json()) as EndUserAuthSuccess;
}

export async function fetchEndUserMe(): Promise<EndUserMeResponse> {
  return (await endUserAuthedJson("/me/end-user")) as EndUserMeResponse;
}

export async function fetchEndUserVendor(slug: string): Promise<EndUserVendor> {
  return (await endUserAuthedJson(
    `/me/end-user/vendors/${encodeURIComponent(slug)}`,
  )) as EndUserVendor;
}

export async function fetchEndUserVendorConversations(
  slug: string,
): Promise<{ vendor: EndUserVendor; conversations: EndUserVendorConversation[] }> {
  return (await endUserAuthedJson(
    `/me/end-user/vendors/${encodeURIComponent(slug)}/conversations`,
  )) as { vendor: EndUserVendor; conversations: EndUserVendorConversation[] };
}

// Widget endpoints. Posting/polling re-uses the Phase 25.4 widget
// endpoints (keyed off widget_public_id) so the consumer surface
// shares the orchestrator with the iframe widget. The end-user
// bearer is attached so widget_conversations.end_user_id gets
// stamped per Phase 25.4.
export async function postWidgetMessageAsEndUser(
  publicId: string,
  body: { text: string; conversation_id?: string },
): Promise<{ conversation_id: string; message_id: number; job_id: string | null }> {
  const token = getEndUserSessionToken();
  const headers: Record<string, string> = {
    "content-type": "application/json",
    // The widget POST endpoint requires Origin to be allowlisted.
    // The /c surface runs on app.lightsei.com which the operator
    // must add to the vendor's allowed_widget_origins. (Defaults
    // are documented for prod; local dev needs the operator to
    // add localhost manually.)
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(
    `${API_URL}/widget/${encodeURIComponent(publicId)}/messages`,
    {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers,
      body: JSON.stringify(body),
    },
  );
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))) as { detail?: unknown };
    throw new Error(
      typeof detail.detail === "string"
        ? detail.detail
        : `send failed (${r.status})`,
    );
  }
  return (await r.json()) as {
    conversation_id: string;
    message_id: number;
    job_id: string | null;
  };
}

export async function fetchWidgetThreadAsEndUser(
  publicId: string,
  conversationId: string,
  since?: number,
): Promise<WidgetThread> {
  const token = getEndUserSessionToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const url = new URL(
    `${API_URL}/widget/${encodeURIComponent(publicId)}/conversations/${encodeURIComponent(conversationId)}`,
  );
  if (since !== undefined) url.searchParams.set("since", String(since));
  const r = await fetch(url.toString(), {
    cache: "no-store",
    credentials: "omit",
    headers,
  });
  if (!r.ok) {
    throw new Error(`thread fetch failed (${r.status})`);
  }
  return (await r.json()) as WidgetThread;
}

// ----- Phase 27.2/27.3: vendor invite codes (operator side) -----

export type VendorInviteCode = {
  code: string;
  workspace_id: string;
  created_at: string;
  expires_at: string;
  consumed_at: string | null;
  consumed_by_end_user_id: string | null;
};

export async function mintVendorInviteCodes(
  count: number,
  ttl_days?: number,
): Promise<{ codes: VendorInviteCode[] }> {
  return (await authedJson("/workspaces/me/end-user-invites", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      count,
      ...(ttl_days !== undefined ? { ttl_days } : {}),
    }),
  })) as { codes: VendorInviteCode[] };
}

export async function fetchVendorInviteCodes(opts?: {
  includeConsumed?: boolean;
  includeExpired?: boolean;
}): Promise<{ codes: VendorInviteCode[] }> {
  const params = new URLSearchParams();
  if (opts?.includeConsumed) params.set("include_consumed", "true");
  if (opts?.includeExpired) params.set("include_expired", "true");
  const qs = params.toString();
  return (await authedJson(
    `/workspaces/me/end-user-invites${qs ? "?" + qs : ""}`,
  )) as { codes: VendorInviteCode[] };
}

export async function revokeVendorInviteCode(
  code: string,
): Promise<{ revoked: boolean; code: string }> {
  return (await authedJson(
    `/workspaces/me/end-user-invites/${encodeURIComponent(code)}`,
    { method: "DELETE" },
  )) as { revoked: boolean; code: string };
}

// ----- Phase 27.4: end-user side of invite codes + vendors-with-counts -----

export type EndUserVendorWithCount = EndUserVendor & {
  unread_count: number;
};

export async function fetchEndUserVendorsWithCounts(): Promise<{
  vendors: EndUserVendorWithCount[];
}> {
  return (await endUserAuthedJson("/me/end-user/vendors")) as {
    vendors: EndUserVendorWithCount[];
  };
}

export type EndUserRedeemResult = {
  linked: boolean;
  vendor: EndUserVendor | null;
  link: {
    linked_at: string;
    linked_via: string;
    notification_pref: string;
    display_name_override: string | null;
  };
};

export async function redeemEndUserInvite(
  code: string,
): Promise<EndUserRedeemResult> {
  return (await endUserAuthedJson("/me/end-user/redeem-invite", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ code }),
  })) as EndUserRedeemResult;
}

export async function patchEndUserVendor(
  workspaceId: string,
  patch: {
    notification_pref?: string;
    display_name_override?: string | null;
  },
): Promise<{
  workspace_id: string;
  notification_pref: string;
  display_name_override: string | null;
}> {
  return (await endUserAuthedJson(
    `/me/end-user/vendors/${encodeURIComponent(workspaceId)}`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    },
  )) as {
    workspace_id: string;
    notification_pref: string;
    display_name_override: string | null;
  };
}

export async function unlinkEndUserVendor(
  workspaceId: string,
): Promise<{ unlinked: boolean; workspace_id: string }> {
  return (await endUserAuthedJson(
    `/me/end-user/vendors/${encodeURIComponent(workspaceId)}`,
    { method: "DELETE" },
  )) as { unlinked: boolean; workspace_id: string };
}

// Phase 28.5: end-user push subscriptions.
//
// `subscribeEndUserPush` ships the keys from
// PushManager.subscribe().toJSON() to the backend, which upserts by
// the (end_user_id, endpoint) composite unique. `unsubscribeEndUserPush`
// soft-revokes (sets revoked_at); the partial active index excludes
// revoked rows from the send fan-out.

export async function subscribeEndUserPush(payload: {
  endpoint: string;
  p256dh: string;
  auth: string;
}): Promise<{ id: string; endpoint: string; active: boolean }> {
  return (await endUserAuthedJson("/me/end-user/push-subscriptions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  })) as { id: string; endpoint: string; active: boolean };
}

export async function unsubscribeEndUserPush(
  endpoint: string,
): Promise<{ revoked: boolean; endpoint: string }> {
  return (await endUserAuthedJson("/me/end-user/push-subscriptions", {
    method: "DELETE",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ endpoint }),
  })) as { revoked: boolean; endpoint: string };
}
