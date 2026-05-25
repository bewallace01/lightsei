// Phase 26.2: end-user session token storage for the /c consumer surface.
//
// Distinct from the operator `lightsei.session_token` so the two never
// confuse each other. Operator dashboard pages keep using getSessionToken
// from api.ts; /c routes use these helpers.
//
// localStorage-backed in v1 so the magic-link consume can land + the
// page reload picks up the token immediately. Phase 26.3 will swap the
// implementation to an httpOnly cookie + parallel server-side helpers
// without changing the call sites.
//
// SSR safe: the localStorage check guards against `window === undefined`
// during Next.js's server-side rendering pass.

const END_USER_TOKEN_KEY = "lightsei.end_user_session";

export function getEndUserToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(END_USER_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setEndUserToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(END_USER_TOKEN_KEY, token);
  } catch {
    // Quota / private mode failures are best-effort; the surfaces
    // calling this should surface an error to the user separately if
    // persistence is critical.
  }
}

export function clearEndUserToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(END_USER_TOKEN_KEY);
  } catch {
    // intentionally swallowed
  }
}
