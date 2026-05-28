"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  clearSession,
  getStoredUser,
  getStoredWorkspace,
  logout as apiLogout,
  SessionUser,
  SessionWorkspace,
} from "./api";
import Logo from "./Logo";
import WorkspaceSwitcher from "./WorkspaceSwitcher";

// Top-level nav items. Either a single `href` link or a `children` dropdown
// group. Order matters — left-to-right rendering.
type NavItem =
  | { kind: "link"; href: string; label: string }
  | {
      kind: "group";
      label: string;
      children: { href: string; label: string; hint?: string }[];
    };

// Phase 18.1: roles-first nav. Top-level reflects what a non-technical
// first-time user is here to do (manage their team, see activity, check
// trust zones, hook up integrations, manage their account). Developer-
// flavored surfaces (drop-a-zip, raw deployments, validators, docs) live
// under an "Advanced" dropdown so the top bar stays uncluttered.
// Polaris is no longer top-level — it's a regular agent in My team.
// Home is reachable via the Logo (which already links to /), so we don't
// need a redundant "home" item.
const NAV: NavItem[] = [
  {
    kind: "group",
    label: "My team",
    children: [
      {
        href: "/agents",
        label: "all agents",
        hint: "Roster, pinned models, schedules, recent activity",
      },
      {
        href: "/agents/team-from-readme",
        label: "✨ propose a team from README",
        hint: "Drop a README, get a constellation",
      },
      {
        href: "/agents/generate",
        label: "✨ generate one bot from a description",
        hint: "Describe a bot, Lightsei generates one",
      },
      {
        href: "/polaris",
        label: "polaris",
        hint: "Workspace orchestrator. Cron-scheduled, dispatches.",
      },
    ],
  },
  {
    kind: "group",
    label: "Activity",
    children: [
      {
        href: "/runs",
        label: "runs",
        hint: "Every LLM call your bots have made",
      },
      {
        href: "/dispatch",
        label: "dispatch chains",
        hint: "Cause-and-effect trees of agent commands",
      },
      {
        href: "/cost",
        label: "cost",
        hint: "MTD spend + per-agent / per-model breakdown",
      },
      {
        href: "/cost/insights",
        label: "✨ cost insights",
        hint: "Where dollars went, what was wasted, one-click fixes",
      },
    ],
  },
  { kind: "link", href: "/zones", label: "Trust zones" },
  { kind: "link", href: "/inbox", label: "Inbox" },
  {
    kind: "group",
    label: "Integrations",
    children: [
      {
        href: "/integrations",
        label: "all integrations",
        hint: "Card grid: every connector + install state",
      },
      {
        href: "/widget-settings",
        label: "widget",
        hint: "Embeddable chat widget for customer-facing conversations",
      },
      {
        href: "/integrations/slack",
        label: "slack",
        hint: "@-mention Lightsei from any channel; per-channel trust zones",
      },
      {
        href: "/integrations/gmail",
        label: "gmail",
        hint: "Send + search email from the connected account",
      },
      {
        href: "/integrations/google-calendar",
        label: "google calendar",
        hint: "Read + write events on the connected calendar",
      },
      {
        href: "/integrations/google-drive",
        label: "google drive",
        hint: "Read, write, search files in the connected drive",
      },
      {
        href: "/notifications",
        label: "notifications",
        hint: "Slack, Discord, Teams, Mattermost, webhook",
      },
      {
        href: "/github",
        label: "github",
        hint: "Push-to-deploy + Polaris doc fetch",
      },
    ],
  },
  { kind: "link", href: "/account", label: "Account" },
  {
    kind: "group",
    label: "Advanced",
    children: [
      {
        href: "/deployments",
        label: "deployments",
        hint: "What the worker is running, plus history",
      },
      {
        href: "/agents/new",
        label: "drop a zip",
        hint: "Upload a pre-zipped bot directory (skip code-gen)",
      },
      {
        href: "/validators",
        label: "validators",
        hint: "Per-event rules: edit, disable, or change mode",
      },
      {
        href: "/getting-started",
        label: "docs",
        hint: "SDK init flow, agent author guide",
      },
    ],
  },
];


function NavLink({
  href,
  label,
  active,
}: {
  href: string;
  label: string;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      className={
        "transition-colors " +
        (active
          ? "text-gray-900 font-medium"
          : "text-gray-500 hover:text-gray-900")
      }
    >
      {label}
    </Link>
  );
}


function NavDropdown({
  label,
  children,
  active,
}: {
  label: string;
  children: { href: string; label: string; hint?: string }[];
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const pathname = usePathname();

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Close on route change so the panel doesn't linger after a nav.
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <div className="relative" ref={wrapRef}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="menu"
        className={
          "flex items-center gap-1 transition-colors " +
          (active
            ? "text-gray-900 font-medium"
            : "text-gray-500 hover:text-gray-900")
        }
      >
        {label}
        <svg
          width="11"
          height="11"
          viewBox="0 0 16 16"
          aria-hidden="true"
          className={"transition-transform " + (open ? "rotate-180" : "")}
        >
          <path
            d="M4 6 L8 10 L12 6"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute left-0 mt-2 w-72 rounded-lg border border-gray-200 bg-white shadow-lg overflow-hidden py-1"
        >
          {children.map((c) => (
            <Link
              key={c.href}
              href={c.href}
              role="menuitem"
              className="block px-3 py-2 hover:bg-gray-50"
            >
              <div className="text-sm text-gray-900">{c.label}</div>
              {c.hint && (
                <div className="text-[11px] text-gray-500 mt-0.5">{c.hint}</div>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}


export default function Header() {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setUser(getStoredUser());
    setWorkspace(getStoredWorkspace());
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  // Close on route change so the menu doesn't linger after a nav.
  useEffect(() => {
    setMenuOpen(false);
    setMobileOpen(false);
  }, [pathname]);

  // Login/signup pages have their own chrome — skip rendering here so we
  // don't double up the logo + "log in / sign up" buttons.
  if (pathname === "/login" || pathname === "/signup") {
    return null;
  }

  // Phase 21.3: the widget iframe at /widget/{public_id} is loaded
  // into the customer's site. It's an anonymous end-user surface,
  // not the Lightsei app — don't render the dashboard header into
  // someone else's product.
  if ((pathname ?? "").startsWith("/widget/")) {
    return null;
  }

  // Phase 26.4: the /c consumer chat surface has its own chrome
  // (no operator nav, no constellation map — per Phase 26 spec).
  // Suppress the dashboard header here too.
  if (pathname === "/c" || (pathname ?? "").startsWith("/c/")) {
    return null;
  }

  const onLogout = async () => {
    try {
      await apiLogout();
    } catch {
      clearSession();
    }
    router.push("/login");
  };

  const isActive = (href: string) => {
    if (href === "/") {
      return pathname === "/";
    }
    return pathname === href || (pathname ?? "").startsWith(href + "/");
  };

  return (
    <header className="sticky top-0 z-20 bg-white/95 backdrop-blur border-b border-gray-100">
      <div className="max-w-6xl mx-auto px-4 sm:px-8 h-14 flex items-center justify-between">
        <Link
          href="/"
          title="Go to dashboard home"
          aria-label="Lightsei — go to dashboard home"
          className="no-underline -ml-2 px-2 py-1 rounded-md hover:bg-gray-100 transition-colors flex items-center"
        >
          <Logo />
        </Link>

        {user ? (
          <>
          {/* Desktop nav: the full row of nav groups + workspace chip
              only fits from md up. Below that it would overflow the
              viewport, so it collapses into the hamburger drawer. */}
          <div className="hidden md:flex items-center gap-6 text-sm">
            <nav className="flex items-center gap-6">
              {NAV.map((n) =>
                n.kind === "link" ? (
                  <NavLink
                    key={n.href}
                    href={n.href}
                    label={n.label}
                    active={isActive(n.href)}
                  />
                ) : (
                  <NavDropdown
                    key={n.label}
                    label={n.label}
                    children={n.children}
                    active={n.children.some((c) => isActive(c.href))}
                  />
                ),
              )}
            </nav>

            <div className="relative" ref={menuRef}>
              <button
                type="button"
                onClick={() => setMenuOpen((o) => !o)}
                aria-expanded={menuOpen}
                aria-haspopup="menu"
                className="flex items-center gap-2 px-2 py-1.5 rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-50 transition-colors"
              >
                <span className="hidden sm:inline max-w-[12rem] truncate">
                  {workspace?.name ?? "Workspace"}
                </span>
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 16 16"
                  aria-hidden="true"
                  className={
                    "transition-transform " + (menuOpen ? "rotate-180" : "")
                  }
                >
                  <path
                    d="M4 6 L8 10 L12 6"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>

              {menuOpen && (
                <div
                  role="menu"
                  className="absolute right-0 mt-2 w-64 rounded-lg border border-gray-200 bg-white shadow-lg overflow-hidden"
                >
                  <div className="px-3 py-3 border-b border-gray-100">
                    <div className="text-[11px] uppercase tracking-wider text-gray-400">
                      Workspace
                    </div>
                    <div className="text-sm font-medium text-gray-900 truncate">
                      {workspace?.name ?? "—"}
                    </div>
                    <div className="text-xs text-gray-500 truncate mt-1">
                      {user.email}
                    </div>
                  </div>
                  {/* Phase 23.4: list + switch + create workspaces.
                      Phase 23.x (#218): onWorkspaceChanged lets the
                      switcher lift the new workspace into Header
                      state so the chip + dropdown title catch up
                      without a page reload. */}
                  <WorkspaceSwitcher
                    onClose={() => setMenuOpen(false)}
                    onWorkspaceChanged={(ws) => setWorkspace(ws)}
                  />
                  <div className="py-1 border-t border-gray-100">
                    <Link
                      href="/workspace-settings"
                      role="menuitem"
                      className="block px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
                    >
                      Workspace settings
                    </Link>
                    <Link
                      href="/account"
                      role="menuitem"
                      className="block px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
                    >
                      Account settings
                    </Link>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={onLogout}
                      className="block w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
                    >
                      Log out
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Mobile: hamburger toggles the full-width drawer below. */}
          <button
            type="button"
            onClick={() => setMobileOpen((o) => !o)}
            aria-expanded={mobileOpen}
            aria-label={mobileOpen ? "Close menu" : "Open menu"}
            className="md:hidden -mr-2 p-2 rounded-md text-gray-600 hover:bg-gray-100 transition-colors"
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              {mobileOpen ? (
                <path
                  d="M6 6 L18 18 M18 6 L6 18"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              ) : (
                <path
                  d="M4 7 H20 M4 12 H20 M4 17 H20"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              )}
            </svg>
          </button>
          </>
        ) : (
          <nav className="flex items-center gap-6 text-sm">
            <Link
              href="/login"
              className="text-gray-500 hover:text-gray-900 transition-colors"
            >
              log in
            </Link>
            <Link
              href="/signup"
              className="px-3 py-1.5 rounded-md bg-accent-600 text-white hover:bg-accent-700 transition-colors no-underline"
            >
              sign up
            </Link>
          </nav>
        )}
      </div>

      {/* Mobile drawer: the full nav expanded vertically + workspace
          actions. Only mounts when signed in + toggled open; the
          md:hidden keeps it off desktop where the inline nav shows. */}
      {user && mobileOpen && (
        <div className="md:hidden border-t border-gray-100 bg-white max-h-[calc(100vh-3.5rem)] overflow-y-auto">
          <nav className="px-4 py-3 flex flex-col gap-1 text-sm">
            {NAV.map((n) =>
              n.kind === "link" ? (
                <Link
                  key={n.href}
                  href={n.href}
                  className={
                    "block px-2 py-2 rounded-md no-underline " +
                    (isActive(n.href)
                      ? "text-gray-900 font-medium bg-gray-50"
                      : "text-gray-600 hover:bg-gray-50")
                  }
                >
                  {n.label}
                </Link>
              ) : (
                <div key={n.label} className="mt-2">
                  <div className="px-2 text-[11px] uppercase tracking-wider text-gray-400">
                    {n.label}
                  </div>
                  {n.children.map((c) => (
                    <Link
                      key={c.href}
                      href={c.href}
                      className={
                        "block px-2 py-2 rounded-md no-underline " +
                        (isActive(c.href)
                          ? "text-gray-900 font-medium bg-gray-50"
                          : "text-gray-600 hover:bg-gray-50")
                      }
                    >
                      {c.label}
                    </Link>
                  ))}
                </div>
              ),
            )}
          </nav>

          <div className="border-t border-gray-100 px-4 py-3">
            <div className="px-2 text-[11px] uppercase tracking-wider text-gray-400">
              Workspace
            </div>
            <div className="px-2 text-sm font-medium text-gray-900 truncate">
              {workspace?.name ?? "—"}
            </div>
            <div className="px-2 text-xs text-gray-500 truncate mb-1">
              {user.email}
            </div>
            <WorkspaceSwitcher
              onClose={() => setMobileOpen(false)}
              onWorkspaceChanged={(ws) => setWorkspace(ws)}
            />
            <Link
              href="/workspace-settings"
              className="block px-2 py-2 rounded-md text-sm text-gray-700 hover:bg-gray-50 no-underline"
            >
              Workspace settings
            </Link>
            <Link
              href="/account"
              className="block px-2 py-2 rounded-md text-sm text-gray-700 hover:bg-gray-50 no-underline"
            >
              Account settings
            </Link>
            <button
              type="button"
              onClick={onLogout}
              className="block w-full text-left px-2 py-2 rounded-md text-sm text-gray-700 hover:bg-gray-50"
            >
              Log out
            </button>
          </div>
        </div>
      )}
    </header>
  );
}
