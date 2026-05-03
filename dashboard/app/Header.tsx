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

const NAV: { href: string; label: string }[] = [
  { href: "/polaris", label: "polaris" },
  { href: "/dispatch", label: "dispatch" },
  { href: "/notifications", label: "notifications" },
  { href: "/github", label: "github" },
];

export default function Header() {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
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
  }, [pathname]);

  // Login/signup pages have their own chrome — skip rendering here so we
  // don't double up the logo + "log in / sign up" buttons.
  if (pathname === "/login" || pathname === "/signup") {
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

  const isActive = (href: string) =>
    pathname === href || (pathname ?? "").startsWith(href + "/");

  return (
    <header className="sticky top-0 z-20 bg-white/95 backdrop-blur border-b border-gray-100">
      <div className="max-w-6xl mx-auto px-6 sm:px-8 h-14 flex items-center justify-between">
        <Link href="/" className="no-underline">
          <Logo />
        </Link>

        {user ? (
          <div className="flex items-center gap-6 text-sm">
            <nav className="flex items-center gap-6">
              {NAV.map((n) => (
                <Link
                  key={n.href}
                  href={n.href}
                  className={
                    "transition-colors " +
                    (isActive(n.href)
                      ? "text-gray-900 font-medium"
                      : "text-gray-500 hover:text-gray-900")
                  }
                >
                  {n.label}
                </Link>
              ))}
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
                  <div className="py-1">
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
    </header>
  );
}
