// Phase 26.3: end-user session token storage for the /c consumer
// surface, parallel to the operator session helpers in api.ts.
//
// Distinct localStorage key from operator (`lightsei.session_token`)
// so the two never collide. Sticks with localStorage in parity with
// the operator side; if the operator session ever moves to httpOnly
// cookies, this module's implementation flips alongside it without
// changing the call sites.
//
// Names match the Phase 26.3 spec: `getEndUserSessionToken`,
// `setEndUserSession`, `clearEndUserSession`. Phase 26.2 used the
// shorter `*Token` names; those are gone now.
//
// SSR safe: the localStorage check guards against `window === undefined`
// during Next.js's server-side rendering pass.

const END_USER_TOKEN_KEY = "lightsei.end_user_session";

export function getEndUserSessionToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(END_USER_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setEndUserSession(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(END_USER_TOKEN_KEY, token);
  } catch {
    // Quota / private mode failures are best-effort; surfaces calling
    // this should surface a user-visible error separately if
    // persistence is critical.
  }
}

export function clearEndUserSession(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(END_USER_TOKEN_KEY);
  } catch {
    // intentionally swallowed
  }
}
