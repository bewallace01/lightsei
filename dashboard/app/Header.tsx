"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  clearSession,
  getStoredUser,
  getStoredWorkspace,
  logout as apiLogout,
  SessionUser,
  SessionWorkspace,
} from "./api";
import Logo from "./Logo";

export default function Header() {
  const router = useRouter();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);

  useEffect(() => {
    setUser(getStoredUser());
    setWorkspace(getStoredWorkspace());
  }, []);

  const onLogout = async () => {
    try {
      await apiLogout();
    } catch {
      clearSession();
    }
    router.push("/login");
  };

  return (
    <header className="flex items-center justify-between mb-10 pb-4 border-b border-gray-100">
      <Link href="/" className="no-underline">
        <Logo />
      </Link>
      <div className="flex items-center gap-5 text-sm text-gray-600">
        {user ? (
          <>
            <span className="hidden sm:inline">
              <span className="font-medium text-gray-900">
                {workspace?.name ?? "—"}
              </span>
              <span className="text-gray-300 mx-2">·</span>
              <span>{user.email}</span>
            </span>
            <Link
              href="/account"
              className="text-gray-700 hover:text-accent-600 transition-colors"
            >
              account
            </Link>
            <button
              type="button"
              onClick={onLogout}
              className="text-gray-700 hover:text-accent-600 transition-colors"
            >
              log out
            </button>
          </>
        ) : (
          <>
            <Link
              href="/login"
              className="text-gray-700 hover:text-accent-600 transition-colors"
            >
              log in
            </Link>
            <Link
              href="/signup"
              className="px-3 py-1.5 rounded-md bg-accent-600 text-white hover:bg-accent-700 transition-colors no-underline"
            >
              sign up
            </Link>
          </>
        )}
      </div>
    </header>
  );
}
